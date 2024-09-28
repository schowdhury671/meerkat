#!/usr/bin/env python
# coding=utf-8
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# You can also adapt this script on your own causal language modeling task. Pointers for this are left as comments.
# import deepspeed
# deepspeed.ops.op_builder.CPUAdamBuilder().load()

import logging
import math
import os
import sys
import datetime
from dataclasses import dataclass, field
from itertools import chain
from typing import Optional

import datasets
import evaluate
import torch
from datasets import load_dataset
from constants import Constants

import transformers
from transformers import (
    CONFIG_MAPPING,
    MODEL_FOR_CAUSAL_LM_MAPPING,
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    HfArgumentParser,
    Trainer,
    TrainingArguments,
    default_data_collator,
    is_torch_tpu_available,
    set_seed,
)
from llm_trainer import LLMTrainer, inference_generation
from transformers.testing_utils import CaptureLogger
from transformers.trainer_utils import get_last_checkpoint
from transformers.utils import check_min_version, send_example_telemetry
from transformers.utils.versions import require_version
# xxx: 2023-03-21
import copy

import torch
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler, TensorDataset
from torch.utils.data.distributed import DistributedSampler
from transformers import CLIPProcessor, CLIPModel, CLIPConfig, LlamaConfig, WhisperConfig, WhisperModel, LlamaModel, LlamaTokenizer, ClapAudioConfig
from transformers import AutoConfig, AutoModel
import torch.distributed as dist
from torch.nn import CrossEntropyLoss

import argparse
import sklearn.metrics as metric
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

from modeling import MM_LLMs, MM_LLMs_Config
import clip
import whisper

try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:
    from tensorboardX import SummaryWriter


json_load = lambda x: json.load(codecs.open(x, 'r', encoding='utf-8'))
json_dump = lambda d, p: json.dump(d, codecs.open(p, 'w', 'utf-8'), indent=2, ensure_ascii=False)


# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
check_min_version("4.27.0.dev0")

require_version("datasets>=1.8.0", "To fix: pip install -r examples/pytorch/language-modeling/requirements.txt")

logger = logging.getLogger(__name__)


MODEL_CONFIG_CLASSES = list(MODEL_FOR_CAUSAL_LM_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)


# xxx: 2023-03-21
IGNORE_INDEX = -100
DEFAULT_PAD_TOKEN = "[PAD]"
DEFAULT_EOS_TOKEN = "</s>"
DEFAULT_BOS_TOKEN = "<s>"
DEFAULT_UNK_TOKEN = "<unk>"


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune, or train from scratch.
    """

    model_name_or_path: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "The model checkpoint for weights initialization.Don't set if you want to train a model from scratch."
            )
        },
    )
    model_type: Optional[str] = field(
        default=None,
        metadata={"help": "If training from scratch, pass a model type from the list: " + ", ".join(MODEL_TYPES)},
    )
    config_overrides: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "Override some existing default config settings when a model is trained from scratch. Example: "
                "n_embd=10,resid_pdrop=0.2,scale_attn_weights=false,summary_type=cls_index"
            )
        },
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={"help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    model_revision: str = field(
        default="main",
        metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": (
                "Will use the token generated when running `huggingface-cli login` (necessary to use this script "
                "with private models)."
            )
        },
    )
    torch_dtype: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "Override the default `torch.dtype` and load the model under this dtype. If `auto` is passed, the "
                "dtype will be automatically derived from the model's weights."
            ),
            "choices": ["auto", "bfloat16", "float16", "float32"],
        },
    )

    n_frames: Optional[int] = field(
        default=1,
        metadata={
            "help": "The number of frames for encoding a video."
        },
    )
    attention_heads: Optional[int] = field(
        # default=220,
        default=128,
        metadata={
            "help": "The number of attention heads used in multi-head-attention."
        },
    )

    image_conv_kernel: Optional[int] = field(
        default=48,
        metadata={
            "help": "The size of the convolutional kernel for the image stream."
        },
    )
    image_conv_stride: Optional[int] = field(
        default=36,
        metadata={
            "help": "The stride of the convolutional kernel for the image stream."
        },
    )
    video_conv_kernel: Optional[int] = field(
        default=36,
        metadata={
            "help": "The size of the convolutional kernel for the video stream."
        },
    )
    video_conv_stride: Optional[int] = field(
        default=30,
        metadata={
            "help": "The stride of the convolutional kernel for the video stream."
        },
    )
    audio_conv_kernel: Optional[int] = field(
        default=240,
        metadata={
            "help": "The size of the convolutional kernel for the audio stream."
        },
    )
    audio_conv_stride: Optional[int] = field(
        default=220,
        metadata={
            "help": "The stride of the convolutional kernel for the audio stream."
        },
    )

    freeze_multi_modal_encoder: bool = field(
        default=False,
        metadata={
            "help": (
                "Whether to freeze the parameters of multi-modal encoders during training.)."
            )
        },
    )
    def __post_init__(self):
        if self.config_overrides is not None and (self.config_name is not None or self.model_name_or_path is not None):
            raise ValueError(
                "--config_overrides can't be used in combination with --config_name or --model_name_or_path"
            )


@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.
    """

    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    dataset_config_name: Optional[str] = field(
        default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
    )
    train_file: Optional[str] = field(default=None, metadata={"help": "The input training data file (a text file)."})
    validation_file: Optional[str] = field(
        default=None,
        metadata={"help": "An optional input evaluation data file to evaluate the perplexity on (a text file)."},
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of training examples to this "
                "value if set."
            )
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
                "value if set."
            )
        },
    )
    streaming: bool = field(default=False, metadata={"help": "Enable streaming mode"})
    block_size: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "Optional input sequence length after tokenization. "
                "The training dataset will be truncated in block of this size for training. "
                "Default to the model max input length for single sentence inputs (take into account special tokens)."
            )
        },
    )
    overwrite_cache: bool = field(
        default=False, metadata={"help": "Overwrite the cached training and evaluation sets"}
    )
    validation_split_percentage: Optional[int] = field(
        default=5,
        metadata={
            "help": "The percentage of the train set used as validation set in case there's no validation split"
        },
    )
    preprocessing_num_workers: Optional[int] = field(
        default=None,
        metadata={"help": "The number of processes to use for the preprocessing."},
    )
    keep_linebreaks: bool = field(
        default=True, metadata={"help": "Whether to keep line breaks when using TXT files or not."}
    )
    is_cross_attn: bool = field(
        default=False, metadata={"help": "Whether to use cross-attention or not."}
    )
    is_simple_prompt: bool = field(
        default=False, metadata={"help": "Whether to use simple prompt or not."}
    )
    is_audio_modality: bool = field(
        default=True, metadata={"help": "Whether to use audio modality or not."}
    )
    use_macaw_ckpt: bool = field(
        default=True, metadata={"help": "Whether to use Macaw pretrained ckpt or not."}
    )
    is_ot: bool = field(
        default=False, metadata={"help": "Whether to use OT or not."}
    )

    def __post_init__(self):
        if self.streaming:
            require_version("datasets>=2.0.0", "The streaming feature requires `datasets>=2.0.0`")

def draw_samples(lis, ratio):
    samples = ratio if ratio > 1 else int(ratio * len(lis))

    if samples > len(lis):
        new_lis = np.random.choice(len(lis), samples, replace=True)
    else:
        new_lis = np.random.choice(len(lis), samples, replace=False)

    n_lis = [lis[i] for i in new_lis]

    return n_lis

def load_datasets(data_args):
    from datasets.dataset_dict import DatasetDict
    from datasets import Dataset

    data_dir = data_args.train_file
    video_names = ["data/train_video_names.json", "data/vqa/vqa_video_names.json"]
    
    all_train_dataset = pickle.load(
        open(data_dir, 'rb'))
    
    pad_token_id = 32006
    all_train_dataset['labels'] = [[(l if l != pad_token_id else IGNORE_INDEX) for l in label] 
    for label in all_train_dataset['labels']]

    vname = json_load(video_names[0])['data']
    vname.extend(json_load(video_names[1])['data'])
    visual_data_names = {'data': vname}

    all_train_dataset['images'] = [[e] for e in all_train_dataset['images']]
    all_train_dataset['audios'] = [[e] for e in all_train_dataset['audios']]
    all_train_dataset['videos'] = [[e] for e in all_train_dataset['videos']]

    eval_offset = 200
    train_dataset = {'train': Dataset.from_dict({k: all_train_dataset[k] for k in all_train_dataset})}

    train_dataset = DatasetDict(train_dataset)
    all_train_dataset = (train_dataset['train'], visual_data_names)

    return all_train_dataset


def prepare_model_for_training(model_args, model, use_gradient_checkpointing=True):
    r"""
    This method wraps the entire protocol for preparing a model before running a training. This includes:
        1- Cast the layernorm in fp32 2- making output embedding layer require grads 3- Add the upcasting of the lm
        head to fp32

    Args:
        model, (`transformers.PreTrainedModel`):
            The loaded model from `transformers`
    """
    loaded_in_8bit = getattr(model, "is_loaded_in_8bit", False)

    for name, param in model.named_parameters():
        # freeze base model's layers
        if ('encoder' in name):
            param.requires_grad = False if model_args.freeze_multi_modal_encoder is True else False
    
    return model


def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.

    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args = parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    Constants.IS_CROSS_ATTN = data_args.is_cross_attn
    Constants.IS_SIMPLE_PROMPT = data_args.is_simple_prompt
    Constants.IS_AUDIO_MODALITY = data_args.is_audio_modality
    Constants.USE_MACAW_CKPT = data_args.use_macaw_ckpt
    Constants.IS_OT = data_args.is_ot


    training_args.remove_unused_columns=False    
    tokenizer = AutoTokenizer.from_pretrained('trained_models/meta_llama_7B')
    

    # Sending telemetry. Tracking the example usage helps us better allocate resources to maintain them. The
    # information sent is the one passed as arguments along with your Python/PyTorch versions.
    send_example_telemetry("run_clm", model_args, data_args)

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if training_args.should_log:
        # The default of training_args.log_level is passive, so we set log level at info here to have that default.
        transformers.utils.logging.set_verbosity_info()

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    logger.info(f"Training/evaluation parameters {training_args}")


    # Set seed before initializing model.
    set_seed(training_args.seed)

    # See more about loading any type of standard or custom dataset (from files, python dict, pandas DataFrame, etc) at
    # https://huggingface.co/docs/datasets/loading_datasets.html.

    # Load pretrained model and tokenizer
    #
    # Distributed training:
    # The .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.

    # load model
    clip_config = CLIPConfig.from_pretrained('openai/clip-vit-base-patch16')
    whisper_config = ClapAudioConfig.from_pretrained('laion/clap-htsat-fused')
    llm_config = AutoConfig.from_pretrained('meta-llama/Llama-2-7b-chat-hf')


    model_config = MM_LLMs_Config(
    n_frames=model_args.n_frames, 
    attention_heads=model_args.attention_heads, 
    image_conv_kernel=model_args.image_conv_kernel, 
    image_conv_stride=model_args.image_conv_stride, 
    video_conv_kernel=model_args.video_conv_kernel, 
    video_conv_stride=model_args.video_conv_stride, 
    audio_conv_kernel=model_args.audio_conv_kernel, 
    audio_conv_stride=model_args.audio_conv_stride,
    clip_config=clip_config, whisper_config=whisper_config, llm_config=llm_config)

    # load model separately 
    model = MM_LLMs(config=model_config)
    model.image_encoder.from_pretrained('openai/clip-vit-base-patch16')
    model.video_encoder.from_pretrained('openai/clip-vit-base-patch16')
    
    model.llm.resize_token_embeddings(len(tokenizer))

    model = prepare_model_for_training(model_args, model)
    
    if training_args.local_rank == 0:
        print(model)
        for name, param in model.named_parameters():
            if param.requires_grad:
                print(name)
    def preprocess_logits_for_metrics(logits, labels):
        if isinstance(logits, tuple):
            # Depending on the model and config, logits may contain extra tensors,
            # like past_key_values, but logits always come first
            logits = logits[0]
        return logits.argmax(dim=-1)
    metric = evaluate.load("accuracy")
    def compute_metrics(eval_preds):
        preds, labels = eval_preds
        # preds have the same shape as the labels, after the argmax(-1) has been calculated
        # by preprocess_logits_for_metrics but we need to shift the labels
        labels = labels[:, 1:].reshape(-1)
        preds = preds[:, :-1].reshape(-1)
        return metric.compute(predictions=preds, references=labels)
    train_dataset, visual_names = load_datasets(data_args)

    if training_args.do_train:
        if data_args.max_train_samples is not None:
            max_train_samples = min(len(train_dataset), data_args.max_train_samples)
            # max_train_samples = 100
            train_dataset = train_dataset.select(range(max_train_samples))
        # xxx: print samples
        logger.info("xxx: Showcase the tokenized training samples.")
        

    # Initialize our Trainer
    trainer = LLMTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset if training_args.do_train else None,
        eval_dataset=None,
        tokenizer=tokenizer,
        # Data collator will default to DataCollatorWithPadding, so we change it.
        data_collator=default_data_collator,
        compute_metrics=compute_metrics,
        preprocess_logits_for_metrics=preprocess_logits_for_metrics if training_args.do_eval 
        and not is_torch_tpu_available() else None,
        )

    def save_checkpoint_callback():
        trainer.save_model()

    # Training
    if training_args.do_train:
        checkpoint = None
        if training_args.resume_from_checkpoint is not None:
            checkpoint = training_args.resume_from_checkpoint
        # elif last_checkpoint is not None:
        #     checkpoint = last_checkpoint
        os.environ["WANDB_DISABLED"] = "true"

        train_result = trainer.train(resume_from_checkpoint=checkpoint)


        trainer.save_model(output_dir=training_args.output_dir)  # Saves the tokenizer too for easy upload

        metrics = train_result.metrics

        max_train_samples = (
            data_args.max_train_samples if data_args.max_train_samples is not None else len(train_dataset)
        )
        metrics["train_samples"] = min(max_train_samples, len(train_dataset))

        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()

    if training_args.do_eval:
        tokenizer = LlamaTokenizer.from_pretrained('trained_models/meta_llama_7B')
        
        model = trainer.get_model()
        image_dirs = ['None', 'None', 'None']
        video_dirs = ['None', 'data/avsd/frames/7UPGT', 'data/avsd/frames/3MSZA']
        audio_dirs = ['None', 'data/avsd/audios/7UPGT', 'data/avsd/audios/3MSZA']

        prompt = "Below is an instruction that describes a task. Write a response that appropriately completes the request.\n\n### Instruction:\n{}\n\n### Response:"
        instructions = [prompt.format('Give three tips for staying healthy.'), 
        prompt.format('Does the woman eat or drink anything?'),
        prompt.format('What\'s on the table next to her?')]

        inference_generation(model, tokenizer, image_dirs, audio_dirs, video_dirs, instructions)


def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()


if __name__ == "__main__":
    main()
