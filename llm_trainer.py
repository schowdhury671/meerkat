from torch import nn
from transformers import Trainer

import torch
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler, TensorDataset
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data import TensorDataset
from transformers import CLIPProcessor, CLIPModel, CLIPConfig, LlamaConfig, WhisperConfig, WhisperModel, LlamaModel, LlamaTokenizer, ClapAudioModel, ClapProcessor
import torch.distributed as dist
from torch.nn import CrossEntropyLoss
from transformers.trainer_utils import ShardedDDPOption
from transformers.deepspeed import deepspeed_init, is_deepspeed_zero3_enabled
from transformers.utils import (
    CONFIG_NAME,
    SAFE_WEIGHTS_INDEX_NAME,
    SAFE_WEIGHTS_NAME,
    WEIGHTS_INDEX_NAME,
    WEIGHTS_NAME,
    can_return_loss,
    find_labels,
    get_full_repo_name,
    is_accelerate_available,
    is_apex_available,
    is_datasets_available,
    is_in_notebook,
    is_ipex_available,
    is_safetensors_available,
    is_sagemaker_dp_enabled,
    is_sagemaker_mp_enabled,
    is_torch_compile_available,
    is_torch_neuroncore_available,
    is_torch_tpu_available,
    logging,
    strtobool,
)

from transformers.trainer_pt_utils import (
    DistributedLengthGroupedSampler,
    DistributedSamplerWithLoop,
    DistributedTensorGatherer,
    IterableDatasetShard,
    LabelSmoother,
    LengthGroupedSampler,
    SequentialDistributedSampler,
    ShardSampler,
    distributed_broadcast_scalars,
    distributed_concat,
    find_batch_size,
    get_model_param_count,
    get_module_class_from_name,
    get_parameter_names,
    nested_concat,
    nested_detach,
    nested_numpify,
    nested_truncate,
    nested_xla_mesh_reduce,
    reissue_pt_warnings,
)

from transformers.modeling_utils import PreTrainedModel, load_sharded_checkpoint, unwrap_model

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union

if is_sagemaker_mp_enabled():
    import smdistributed.modelparallel.torch as smp
    from smdistributed.modelparallel import __version__ as SMP_VERSION

    IS_SAGEMAKER_MP_POST_1_10 = version.parse(SMP_VERSION) >= version.parse("1.10")

    from transformers.trainer_pt_utils import smp_forward_backward, smp_forward_only, smp_gather, smp_nested_concat
else:
    IS_SAGEMAKER_MP_POST_1_10 = False

TRAINING_ARGS_NAME = "training_args.bin"
TRAINER_STATE_NAME = "trainer_state.json"
OPTIMIZER_NAME = "optimizer.pt"
SCHEDULER_NAME = "scheduler.pt"
SCALER_NAME = "scaler.pt"

import argparse
import sklearn.metrics as metric
from constants import Constants
import glob
import logging
import os
import random
import numpy as np
import json
import pickle
import codecs
from PIL import Image
from peft import PeftModel
from torchvision.transforms import Compose, Resize, CenterCrop, ToTensor, Normalize
try:
    from torchvision.transforms import InterpolationMode
    BICUBIC = InterpolationMode.BICUBIC
except ImportError:
    BICUBIC = Image.BICUBIC
from tqdm import tqdm, trange
from sklearn.metrics import top_k_accuracy_score
from transformers import (
    WEIGHTS_NAME,
    AdamW,
    get_linear_schedule_with_warmup,
)

from peft import (
    LoraConfig,
    get_peft_model,
    get_peft_model_state_dict,
    # prepare_model_for_int8_training,
    set_peft_model_state_dict,
)

# from modeling import MM_LLMs, MM_LLMs_Config
import clip
import whisper

try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:
    from tensorboardX import SummaryWriter

json_load = lambda x: json.load(codecs.open(x, 'r', encoding='utf-8'))
json_dump = lambda d, p: json.dump(d, codecs.open(p, 'w', 'utf-8'), indent=2, ensure_ascii=False)

special_tokens = {
    '<image>': 32000,
    '</image>': 32001,
    '<audio>': 32002,
    '</audio>': 32003,
    '<video>': 32004,
    '</video>': 32005,
}

AUDIOSET_MISSING_FILE_MAPPING = json_load('data/openimages/audioset_missing_file_mapping_v2.json') # change `final_data/` to `data/`
VGGSOUND_MISSING_FILE_MAPPING = json_load('data/openimages/vggsound_missing_file_mapping_v2.json')
VALID_AUDIOS_LIST = os.listdir('data/audioset_sreyan/unbalanced_train')
VALID_AUDIOS_LIST_VGGSOUND = os.listdir('data/vggsound/train')

all_visual_names_json = json_load('data/all_visual_names.json')['dict']
all_visual_names_keys = list(all_visual_names_json.keys())
all_visual_names_values = list(all_visual_names_json.values())

def draw_samples(lis, ratio):
    samples = ratio if ratio > 1 else int(ratio * len(lis))

    if samples > len(lis):
        new_lis = np.random.choice(len(lis), samples, replace=True)
    else:
        new_lis = np.random.choice(len(lis), samples, replace=False)

    n_lis = [lis[i] for i in new_lis]

    return n_lis

def _convert_image_to_rgb(image):
    return image.convert("RGB")


def _transform(n_px):
    return Compose([
        Resize((n_px, n_px), interpolation=BICUBIC),
        # CenterCrop(n_px),
        _convert_image_to_rgb,
        ToTensor(),
        Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)),
    ])


visual_name_dir = "data/all_visual_names_instruction.json"
vname = json_load(visual_name_dir)['list']
train_video_names = {'data': vname}

preprocess = _transform(224)
device = torch.device("cuda")


t_frames = 120
interval = t_frames // 6

frame_ind = [i * interval for i in range(6)]
for i in range(len(frame_ind)):
    if frame_ind[i] >= t_frames:
        frame_ind[i] = t_frames - 1
frame_ind[-1] = t_frames - 1

frame_ind = [0]
train_frame_ind = frame_ind

class LLMTrainer(Trainer):
    def compute_loss(self, model, inputs, return_outputs=False):
        inputs = self.get_self_inputs(inputs, model)
        # forward pass
        loss = model(**inputs, return_alignment_loss=True) 
        loss = loss[0] 
        
        return loss


    def prediction_step(
        self,
        model: nn.Module,
        inputs: Dict[str, Union[torch.Tensor, Any]],
        prediction_loss_only: bool,
        ignore_keys: Optional[List[str]] = None,
    ) -> Tuple[Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]]:
        """
        Perform an evaluation step on `model` using `inputs`.

        Subclass and override to inject custom behavior.

        Args:
            model (`nn.Module`):
                The model to evaluate.
            inputs (`Dict[str, Union[torch.Tensor, Any]]`):
                The inputs and targets of the model.

                The dictionary will be unpacked before being fed to the model. Most models expect the targets under the
                argument `labels`. Check your model's documentation for all accepted arguments.
            prediction_loss_only (`bool`):
                Whether or not to return the loss only.
            ignore_keys (`List[str]`, *optional*):
                A list of keys in the output of your model (if it is a dictionary) that should be ignored when
                gathering predictions.

        Return:
            Tuple[Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]]: A tuple with the loss,
            logits and labels (each being optional).
        """
        has_labels = False if len(self.label_names) == 0 else all(inputs.get(k) is not None for k in self.label_names)
        # For CLIP-like models capable of returning loss values.
        # If `return_loss` is not specified or being `None` in `inputs`, we check if the default value of `return_loss`
        # is `True` in `model.forward`.
        return_loss = inputs.get("return_loss", None)
        if return_loss is None:
            return_loss = self.can_return_loss
        loss_without_labels = True if len(self.label_names) == 0 and return_loss else False

        inputs = self._prepare_inputs(inputs)
        if ignore_keys is None:
            if hasattr(self.model, "config"):
                ignore_keys = getattr(self.model.config, "keys_to_ignore_at_inference", [])
            else:
                ignore_keys = []

        # labels may be popped when computing the loss (label smoothing for instance) so we grab them first.
        if has_labels or loss_without_labels:
            labels = nested_detach(tuple(inputs.get(name) for name in self.label_names))
            if len(labels) == 1:
                labels = labels[0]
        else:
            labels = None

        with torch.no_grad():
            if is_sagemaker_mp_enabled():
                raw_outputs = smp_forward_only(model, inputs)
                if has_labels or loss_without_labels:
                    if isinstance(raw_outputs, dict):
                        loss_mb = raw_outputs["loss"]
                        logits_mb = tuple(v for k, v in raw_outputs.items() if k not in ignore_keys + ["loss"])
                    else:
                        loss_mb = raw_outputs[0]
                        logits_mb = raw_outputs[1:]

                    loss = loss_mb.reduce_mean().detach().cpu()
                    logits = smp_nested_concat(logits_mb)
                else:
                    loss = None
                    if isinstance(raw_outputs, dict):
                        logits_mb = tuple(v for k, v in raw_outputs.items() if k not in ignore_keys)
                    else:
                        logits_mb = raw_outputs
                    logits = smp_nested_concat(logits_mb)
            else:
                if has_labels or loss_without_labels:
                    with self.compute_loss_context_manager():
                        loss, outputs = self.compute_loss(model, inputs, return_outputs=True)
                    loss = loss.mean().detach()

                    if isinstance(outputs, dict):
                        logits = tuple(v for k, v in outputs.items() if k not in ignore_keys + ["loss"])
                    else:
                        logits = outputs[1:]
                else:
                    loss = None
                    with self.compute_loss_context_manager():
                        inputs = self.get_self_inputs(inputs, model)
                        # forward pass
                        outputs = model(**inputs)

                        logits = outputs[1]
                        max_index = torch.argmax(logits.softmax(dim=-1), dim=-1).view(logits.size(0), -1).to(device)

                        input_text = self.tokenizer.batch_decode(max_index, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
                    if isinstance(outputs, dict):
                        logits = tuple(v for k, v in outputs.items() if k not in ignore_keys)
                    else:
                        logits = outputs
                    # TODO: this needs to be fixed and made cleaner later.
                    if self.args.past_index >= 0:
                        self._past = outputs[self.args.past_index - 1]

        if prediction_loss_only:
            return (loss, None, None)

        logits = nested_detach(logits)
        if len(logits) == 1:
            logits = logits[0]

        return (loss, logits, labels)

    def get_self_inputs(self, batch, model):
        train_video_ind = list(batch['videos'].cpu().numpy())
        

        all_video_frames = []
        for vid in train_video_ind:
            vid = vid[0]
            _all_video_frames = []
            for vfi in train_frame_ind:
                if vid == -1:
                    _all_video_frames.append(torch.zeros(1, 3, 224, 224))
                    continue
                
                vid_key = all_visual_names_keys[all_visual_names_values.index(vid)]
                try:
                    if 'avsbench_' in vid_key:
                        dataset_name = 'avsbench'
                    elif 'vggss_' in vid_key:
                        dataset_name = 'vggss'
                    elif 'soundnet_' in vid_key:
                        dataset_name = 'soundnet'
                    elif 'llp_' in vid_key:
                        dataset_name = 'llp'
                    elif 'avqa_' in vid_key and 'music_avqa_' not in vid_key:
                        dataset_name = 'avqa'
                    elif 'music_avqa_' in vid_key:
                        dataset_name = 'music_avqa'
                    elif 'valor_' in vid_key:
                        dataset_name = 'valor'
                    elif 'sed_' in vid_key:
                        dataset_name = 'sed'
                    elif 'clotho_' in vid_key:
                        raise Exception
                    elif 'openimages_' in vid_key:
                        dataset_name = 'openimages'
                    elif 'audioset_temporal_' in vid_key:
                        dataset_name = 'openimages'
                    elif 'multimodal_factchecking' in  vid_key:
                        dataset_name = 'openimages'
                    elif 'ave_' in vid_key and 'openimages_' not in vid_key:
                        dataset_name = 'ave'
                    else:
                        dataset_name = 'avsd'
                    
                    if 'openimages' in vid_key or 'multimodal_factchecking' in  vid_key:
                        fname = vid_key.split('#')[0]
                        fname  = fname.split('_')[-1]
                        yt_id = vid_key.split('#')[-1]

                        image_dir = f'data/{dataset_name}/frames/'
                        frame = preprocess(
                            Image.open('{}{}.jpg'.format(image_dir, fname))
                            )
                    else:
                        image_dir = f'data/{dataset_name}/frames/'
                        vid_key = vid_key.split('#')[0]
                        frame = preprocess(
                            Image.open('{}{}.mp4_{}.jpg'.format(image_dir, vid_key, str(vfi)))
                            )
                except:
                    frame = torch.zeros(3, 224, 224)
                _all_video_frames.append(frame.unsqueeze(0))
            _all_video_frames = torch.cat(_all_video_frames, dim=0).unsqueeze(0)
            all_video_frames.append(_all_video_frames)
        all_video_frames = torch.cat(all_video_frames, dim=0)

        train_audio_ind = list(batch['audios'].cpu().numpy())

        all_audio_mels = []

        for aid in train_audio_ind:
            aid = aid[0]
            if aid == -1:
                all_audio_mels.append(torch.zeros(1, 4, 1001, 64))
                continue
            # load audio and pad/trim it to fit 30 seconds
            aud_key = all_visual_names_keys[all_visual_names_values.index(aid)]
            if 'avsbench_' in aud_key:
                dataset_name = 'avsbench'
            elif 'vggss_' in aud_key:
                dataset_name = 'vggss'
            elif 'soundnet_' in aud_key:
                dataset_name = 'soundnet'
            elif 'llp_' in aud_key:
                dataset_name = 'llp'
            elif 'avqa_' in aud_key and 'music_avqa_' not in aud_key:
                dataset_name = 'avqa'
            elif 'music_avqa_' in aud_key:
                dataset_name = 'music_avqa'
            elif 'valor_' in aud_key:
                dataset_name = 'valor'
            elif 'sed_' in aud_key:
                dataset_name = 'sed'
            elif 'clotho_' in aud_key:
                dataset_name = 'clotho'
            elif 'openimages_' in aud_key and 'openimages_vggsound_' not in aud_key:
                dataset_name = 'audioset_sreyan'
            elif 'openimages_vggsound_' in aud_key:
                dataset_name = 'vggsound'
            elif 'audioset_temporal_' in aud_key:
                dataset_name = 'audioset_temporal'
            elif 'multimodal_factchecking_type4' in  aud_key or 'multimodal_factchecking_type3' in aud_key or 'multimodal_factchecking_type2' in aud_key:
                dataset_name = 'audioset_temporal'
            elif 'multimodal_factchecking_type1' in  aud_key:
                dataset_name = 'audioset_sreyan'
            elif 'ave_' in aud_key and 'openimages_' not in aud_key:
                dataset_name = 'ave'
            else:
                dataset_name = 'avsd'

        
            if ('openimages_' in aud_key and 'openimages_vggsound_' not in aud_key) or ('multimodal_factchecking_type1' in  aud_key):
                
                yt_id = aud_key.split('#')[-1]
                label = aud_key.split('#')[1]
                label = label.replace('$', '/')

                try:
                    if f'{yt_id}.wav' not in VALID_AUDIOS_LIST:
                        raise Exception
                
                    audio_dir = f'data/{dataset_name}/audios/'
                    audio = whisper.load_audio("{}{}.wav".format(audio_dir, yt_id))
                except:
                    error_file_path = f'data/{dataset_name}/corrupted_audios.txt'
                    
                    try:
                        with open(error_file_path, 'a') as file:
                            # Write the line to the file
                            file.write(f'{yt_id}.wav\n')
                    except Exception as e:
                        print(f"Error writing to {error_file_path}: {e}")

                    
                    yt_id = AUDIOSET_MISSING_FILE_MAPPING[label][:-4]
                
                    audio_dir = f'data/{dataset_name}/audios/'

                    audio = whisper.load_audio("{}{}.wav".format(audio_dir, yt_id))

            elif 'audioset_temporal_' in aud_key or 'multimodal_factchecking_type4' in  aud_key or 'multimodal_factchecking_type3' in aud_key or 'multimodal_factchecking_type2' in aud_key:
                
                yt_id = aud_key.split('#')[-1]
                label = aud_key.split('#')[1]
                label = label.replace('$', '/')
                
                try:
                    audio_dir = f'data/{dataset_name}/audios/'
                    audio = whisper.load_audio("{}{}.wav".format(audio_dir, yt_id))
                except:
                    yt_id = AUDIOSET_MISSING_FILE_MAPPING[label][:-4]
                
                    audio_dir = f'data/{dataset_name}/audios/'

                    audio = whisper.load_audio("{}{}.wav".format(audio_dir, yt_id))

            elif 'openimages_vggsound_' in aud_key:                
                yt_id = aud_key.split('#')[-1]
                label = aud_key.split('#')[1]
                label = label.replace('$', '/')

                try:
                    if f'{yt_id}.wav' not in VALID_AUDIOS_LIST_VGGSOUND:
                        raise Exception
                
                    audio_dir = f'data/{dataset_name}/audios/'
                    audio = whisper.load_audio("{}{}.wav".format(audio_dir, yt_id))
                except:
                    error_file_path = f'data/{dataset_name}/corrupted_audios.txt'
                    
                    try:
                        with open(error_file_path, 'a') as file:
                            # Write the line to the file
                            file.write(f'{yt_id}.wav\n')
                    except Exception as e:
                        print(f"Error writing to {error_file_path}: {e}")

                    yt_id = VGGSOUND_MISSING_FILE_MAPPING[label][:-4]
                
                    audio_dir = f'data/{dataset_name}/train/'
                    audio = whisper.load_audio("{}{}.wav".format(audio_dir, yt_id))

                    
            else:                
                audio_dir = f'data/{dataset_name}/audios/'
                aud_key = aud_key.split('#')[0]
                audio = whisper.load_audio("{}{}.mp4.wav".format(audio_dir, aud_key))
                
            audio = whisper.pad_or_trim(audio)

            audio = model.audio_processor(audios=audio, return_tensors="pt")
            mel = audio['input_features']
            all_audio_mels.append(mel)
        all_audio_mels = torch.cat(all_audio_mels, dim=0)


        all_images = []
        train_image_ind = list(batch['images'].cpu().numpy())
        for vid in train_image_ind:
            vid = vid[0]
            if vid == -1:
                all_images.append(torch.zeros(1, 3, 224, 224))
                continue
            try:
                _image_dir = train_video_names['data'][vid]
                
                frame = preprocess(Image.open('data/coco/train2014/{}'.format(_image_dir)))
                all_images.append(frame.unsqueeze(0))
            except:
                print('Image not found')
        
        all_images = torch.cat(all_images, dim=0)

        bs = len(train_video_ind)

        inputs = {
                    'videos': all_video_frames.half(),
                    'audios': all_audio_mels.half(),
                    'images': all_images.half(),
                    'input_ids': batch['input_ids'],
                    'attention_mask': batch['attention_mask'],
                    'labels': batch['labels'] if 'labels' in batch else None,
                    'image_starts': torch.tensor([self.tokenizer.convert_tokens_to_ids('<image>')] * bs, dtype=torch.int),
                    'image_ends': torch.tensor([self.tokenizer.convert_tokens_to_ids('</image>')] * bs, dtype=torch.int),
                    'audio_starts': torch.tensor([self.tokenizer.convert_tokens_to_ids('<audio>')] * bs, dtype=torch.int),
                    'audio_ends': torch.tensor([self.tokenizer.convert_tokens_to_ids('</audio>')] * bs, dtype=torch.int),
                    'video_starts': torch.tensor([self.tokenizer.convert_tokens_to_ids('<video>')] * bs, dtype=torch.int),
                    'video_ends': torch.tensor([self.tokenizer.convert_tokens_to_ids('</video>')] * bs, dtype=torch.int),
                    }
        
        inputs = {k: inputs[k].to(device) if inputs[k] is not None else inputs[k] for k in inputs}

        return {'inputs': inputs}


    def get_model(self):
        """
        return model module
        """
        args = self.args

        eval_dataloader = self.get_eval_dataloader()

        # if eval is called w/o train init deepspeed here
        if args.deepspeed and not self.deepspeed:
            # XXX: eval doesn't have `resume_from_checkpoint` arg but we should be able to do eval
            # from the checkpoint eventually
            deepspeed_engine, _, _ = deepspeed_init(self, num_training_steps=0, resume_from_checkpoint=None)
            self.model = deepspeed_engine.module
            self.model_wrapped = deepspeed_engine
            self.deepspeed = deepspeed_engine
            # XXX: we don't need optim/sched for inference, but this needs to be sorted out, since
            # for example the Z3-optimizer is a must for zero3 to work even for inference - what we
            # don't need is the deepspeed basic optimizer which is self.optimizer.optimizer
            deepspeed_engine.optimizer.optimizer = None
            deepspeed_engine.lr_scheduler = None

        model = self._wrap_model(self.model, training=False, dataloader=eval_dataloader)

        # if full fp16 or bf16 eval is wanted and this ``evaluation`` or ``predict`` isn't called
        # while ``train`` is running, cast it to the right dtype first and then put on device
        if not self.is_in_train:
            if args.fp16_full_eval:
                model = model.to(dtype=torch.float16, device=args.device)
            elif args.bf16_full_eval:
                model = model.to(dtype=torch.bfloat16, device=args.device)
        model.eval()

        return model


def inference_generation(args, model, tokenizer, image_dirs, audio_dirs, video_dirs, instructions, responses, dataset):
    with torch.no_grad():
        all_eval_outs = []
        for image_dir, video_dir, audio_dir, instruction, true_response in tqdm(zip(image_dirs, video_dirs, audio_dirs, instructions, responses)):
            _all_video_frames = []
            for vfi in train_frame_ind:
                if video_dir == 'None':
                    _all_video_frames.append(torch.zeros(1, 3, 224, 224))
                    continue
                vfi = 0
                frame = preprocess(
                    Image.open('{}.mp4_{}.jpg'.format(video_dir, str(vfi))))
                _all_video_frames.append(frame.unsqueeze(0))
            all_video_frames = torch.cat(_all_video_frames, dim=0).unsqueeze(0)

            if audio_dir == 'None':
                all_audio_mels = torch.zeros(1, 80, 3000)
            else:
                # load audio and pad/trim it to fit 30 seconds
                audio = whisper.load_audio(audio_dir)

                audio = whisper.pad_or_trim(audio)

                audio = model.audio_processor(audios=audio, return_tensors="pt")
                mel = audio['input_features']

                all_audio_mels = mel
            
            all_images = []
            if image_dir == 'None':
                all_images = torch.zeros(1, 3, 224, 224)
            else:

                frame = preprocess(Image.open(image_dir))
                all_images = frame.unsqueeze(0)

            input_ids = tokenizer.encode(instruction)
            
            eos_token_id = tokenizer.eos_token_id
            if eos_token_id in input_ids:
                input_ids.remove(eos_token_id)

            input_ids = torch.tensor([input_ids], dtype=torch.int).to(device)

            bs = all_video_frames.size(0)
            seq_len = input_ids.size(1)

            inputs = {'videos': all_video_frames.half(),
                    'audios': all_audio_mels.half(),
                    'images': all_images.half(),
                    'input_ids': input_ids,
                    'image_starts': torch.tensor([tokenizer.convert_tokens_to_ids('<image>')] * bs, dtype=torch.int),
                    'image_ends': torch.tensor([tokenizer.convert_tokens_to_ids('</image>')] * bs, dtype=torch.int),
                    'audio_starts': torch.tensor([tokenizer.convert_tokens_to_ids('<audio>')] * bs, dtype=torch.int),
                    'audio_ends': torch.tensor([tokenizer.convert_tokens_to_ids('</audio>')] * bs, dtype=torch.int),
                    'video_starts': torch.tensor([tokenizer.convert_tokens_to_ids('<video>')] * bs, dtype=torch.int),
                    'video_ends': torch.tensor([tokenizer.convert_tokens_to_ids('</video>')] * bs, dtype=torch.int),
                    }
            inputs = {k: inputs[k].to(device) for k in inputs}

            inputs['inference'] = True
            try:
                generate_ids = model(inputs)
            except Exception as e:
                continue

            input_text = tokenizer.batch_decode(input_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]

            generated_text = tokenizer.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]

            e = {
                'image_dir': image_dir,
                'video_dir': video_dir,
                'audio_dir': audio_dir, 
                'instruction': instruction,
                'input': input_text,
                'output': generated_text.strip(),
                'true_response': true_response
            }
            all_eval_outs.append(e)
        json_dump({"outputs": all_eval_outs}, 'eval_outputs/{}_eval_outputs_1by1.json'.format(dataset))


def inference_generation_openimages(args, model, tokenizer, image_dirs, audio_dirs, video_dirs, instructions, responses, dataset):
    with torch.no_grad():
        all_eval_outs = []
        for image_dir, video_dir, audio_dir, instruction, true_response in tqdm(zip(image_dirs, video_dirs, audio_dirs, instructions, responses)):
            _all_video_frames = []
            
            missing_flag = 0
            for vfi in train_frame_ind:
                if not os.path.exists(audio_dir):
                    print('\n\n\n AUDIO PATH DOES NOT EXIST!!! Skipping image. \n\n\n')
                    print(audio_dir)
                    missing_flag = 1
                    break
                if video_dir == 'None':
                    _all_video_frames.append(torch.zeros(1, 3, 224, 224))
                    continue
                vfi = 0
                frame = preprocess(
                    Image.open('{}.jpg'.format(video_dir, str(vfi))))
                _all_video_frames.append(frame.unsqueeze(0))
            
            if missing_flag == 0:
                all_video_frames = torch.cat(_all_video_frames, dim=0).unsqueeze(0)
            else:
                continue

            if audio_dir == 'None':
                all_audio_mels = torch.zeros(1, 80, 3000)
            else:
                if not os.path.exists(audio_dir):
                    print('\n\n\n AUDIO PATH DOES NOT EXIST!!! \n\n\n')
                    print(audio_dir)
                    continue
                
                try:
                    # load audio and pad/trim it to fit 30 seconds
                    audio = whisper.load_audio(audio_dir)

                    audio = whisper.pad_or_trim(audio)

                    audio = model.audio_processor(audios=audio, return_tensors="pt")
                    mel = audio['input_features']
                    is_audio_loaded_properly = True
                except:
                    mel = torch.zeros(1, 4, 1001, 64)
                    is_audio_loaded_properly = False

                all_audio_mels = mel
            
            all_images = []
            if image_dir == 'None':
                all_images = torch.zeros(1, 3, 224, 224)
            else:

                frame = preprocess(Image.open(image_dir))
                all_images = frame.unsqueeze(0)

            input_ids = tokenizer.encode(instruction)
            
            eos_token_id = tokenizer.eos_token_id
            if eos_token_id in input_ids:
                input_ids.remove(eos_token_id)

            input_ids = torch.tensor([input_ids], dtype=torch.int).to(device)

            bs = all_video_frames.size(0)
            seq_len = input_ids.size(1)

            inputs = {'videos': all_video_frames.half(),
                    'audios': all_audio_mels.half(),
                    'images': all_images.half(),
                    'input_ids': input_ids,
                    'image_starts': torch.tensor([tokenizer.convert_tokens_to_ids('<image>')] * bs, dtype=torch.int),
                    'image_ends': torch.tensor([tokenizer.convert_tokens_to_ids('</image>')] * bs, dtype=torch.int),
                    'audio_starts': torch.tensor([tokenizer.convert_tokens_to_ids('<audio>')] * bs, dtype=torch.int),
                    'audio_ends': torch.tensor([tokenizer.convert_tokens_to_ids('</audio>')] * bs, dtype=torch.int),
                    'video_starts': torch.tensor([tokenizer.convert_tokens_to_ids('<video>')] * bs, dtype=torch.int),
                    'video_ends': torch.tensor([tokenizer.convert_tokens_to_ids('</video>')] * bs, dtype=torch.int),
                    }
            inputs = {k: inputs[k].to(device) for k in inputs}

            inputs['inference'] = True
            try:
                generate_ids = model(inputs)
            except Exception as e:
                continue

            input_text = tokenizer.batch_decode(input_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]

            generated_text = tokenizer.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]

            e = {
                'image_dir': image_dir,
                'video_dir': video_dir,
                'audio_dir': audio_dir, 
                'instruction': instruction,
                'input': input_text,
                'output': generated_text.strip(),
                'true_response': true_response,
                'is_audio_loaded_properly': is_audio_loaded_properly
            }
            all_eval_outs.append(e)
        json_dump({"outputs": all_eval_outs}, 'eval_outputs/{}_eval_outputs_1by1_22Jun.json'.format(dataset))

def batch_inference_generation(args, model, tokenizer, image_dirs, audio_dirs, video_dirs, instructions, responses, batch_size, dataset):
    all_eval_outs = []
    with torch.no_grad():
        num_examples = len(image_dirs)
        for i in tqdm(range(0, num_examples, batch_size)):
            batch_image_dirs = image_dirs[i:i+batch_size]
            batch_audio_dirs = audio_dirs[i:i+batch_size]
            batch_video_dirs = video_dirs[i:i+batch_size]
            batch_instructions = instructions[i:i+batch_size]
            batch_responses = responses[i:i+batch_size]

            batch_all_video_frames = []
            batch_all_audio_mels = []
            batch_all_images = []
            batch_input_ids = []
            batch_attention_masks = []

            for image_dir, video_dir, audio_dir, instruction in zip(batch_image_dirs, batch_video_dirs, batch_audio_dirs, batch_instructions):
                _all_video_frames = []
                for vfi in train_frame_ind:
                    if video_dir == 'None':
                        _all_video_frames.append(torch.zeros(1, 3, 224, 224))
                        continue
                    frame = preprocess(
                        Image.open('{}.mp4_{}.jpg'.format(video_dir, str(vfi))))
                    _all_video_frames.append(frame.unsqueeze(0))
                all_video_frames = torch.cat(_all_video_frames, dim=0).unsqueeze(0)
                batch_all_video_frames.append(all_video_frames)

                if audio_dir == 'None':
                    all_audio_mels = torch.zeros(1, 80, 3000)
                else:
                    # print(audio_dir)
                    audio = whisper.load_audio(audio_dir)
                    audio = whisper.pad_or_trim(audio)
                    mel = whisper.log_mel_spectrogram(audio)
                    all_audio_mels = mel.unsqueeze(0)
                batch_all_audio_mels.append(all_audio_mels)

                if image_dir == 'None':
                    all_images = torch.zeros(1, 3, 224, 224)
                else:
                    frame = preprocess(Image.open(image_dir))
                    all_images = frame.unsqueeze(0)
                batch_all_images.append(all_images)

            max_length = 256
            tokenized_outputs = tokenizer(batch_instructions, max_length=max_length, padding='max_length', truncation=True)
            batch_input_ids = torch.tensor(tokenized_outputs['input_ids'], dtype=torch.int).to(device)
            # batch_input_ids.append(input_ids)

            batch_attention_masks = torch.tensor(tokenized_outputs['attention_mask'], dtype=torch.int).to(device)
            # batch_attention_masks.append(attention_masks)

            # Stack tensors
            batch_all_video_frames = torch.cat(batch_all_video_frames, dim=0)
            batch_all_audio_mels = torch.cat(batch_all_audio_mels, dim=0)
            batch_all_images = torch.cat(batch_all_images, dim=0)
            # batch_input_ids = torch.cat(batch_input_ids, dim=0)
            # batch_attention_masks = torch.cat(batch_attention_masks, dim=0)

            bs = batch_all_video_frames.size(0)
            seq_len = batch_input_ids.size(1)

            inputs = {'videos': batch_all_video_frames.half(),
                      'audios': batch_all_audio_mels.half(),
                      'images': batch_all_images.half(),
                      'input_ids': batch_input_ids,
                      'image_starts': torch.tensor([tokenizer.convert_tokens_to_ids('<image>')] * bs, dtype=torch.int),
                      'image_ends': torch.tensor([tokenizer.convert_tokens_to_ids('</image>')] * bs, dtype=torch.int),
                      'audio_starts': torch.tensor([tokenizer.convert_tokens_to_ids('<audio>')] * bs, dtype=torch.int),
                      'audio_ends': torch.tensor([tokenizer.convert_tokens_to_ids('</audio>')] * bs, dtype=torch.int),
                      'video_starts': torch.tensor([tokenizer.convert_tokens_to_ids('<video>')] * bs, dtype=torch.int),
                      'video_ends': torch.tensor([tokenizer.convert_tokens_to_ids('</video>')] * bs, dtype=torch.int),
                      }
            inputs = {k: inputs[k].to(device) for k in inputs}
            inputs['inference'] = True

            try:
                generate_ids = model(inputs)
            except Exception as ee:
                continue

            input_texts = tokenizer.batch_decode(batch_input_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
            generated_texts = tokenizer.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)

            for image_dir, video_dir, audio_dir, instruction, input_text, generated_text, true_response in zip(batch_image_dirs, batch_video_dirs, batch_audio_dirs, batch_instructions, input_texts, generated_texts, batch_responses):
                e = {
                    'image_dir': image_dir,
                    'video_dir': video_dir,
                    'audio_dir': audio_dir, 
                    'instruction': instruction,
                    'input': input_text,
                    'output': generated_text.strip(),
                    'true_response': true_response
                }
                all_eval_outs.append(e)

            if args.local_rank == 0 or args.local_rank == -1:
                post_fix = args.output_dir.split("mm_llms_trainer_")[-1].replace('/', '_')
                json_dump(all_eval_outs, 'eval_outputs/{}_eval_outputs_{}.json'.format(dataset, post_fix))