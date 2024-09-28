# export NCCL_DEBUG=INFO
# export NCCL_SOCKET_IFNAME=eth1
# export NCCL_IB_GID_INDEX=3
# export NCCL_IB_SL=3
# export NCCL_NET_GDR_READ=1

export CHIEF_IP=127.0.0.1
export MASTER_ADDR="${CHIEF_IP:=localhost}"
export MASTER_PORT="${MASTER_PORT:=29500}"

path=./
train_path=$path/run_clm_llms.py


#  SUPERVISED TRAINING
CUDA_VISIBLE_DEVICES=0,1,2,3 torchrun --nnodes 1 --nproc_per_node 4 \
    ${train_path} \
    --model_name_or_path ${path} \
    --deepspeed $path/configs/deepspeed_config.json \
    --train_file data/all_data.cache \
    --preprocessing_num_workers 2 \
    --per_device_train_batch_size 2 \
    --per_device_eval_batch_size 1 \
    --gradient_accumulation_steps 16 \
    --num_train_epochs 3 \
    --save_strategy "steps" \
    --save_steps 5000 \
    --save_total_limit 1 \
    --learning_rate 3e-5 \
    --weight_decay 0. \
    --warmup_ratio 0.03 \
    --lr_scheduler_type "cosine" \
    --logging_steps 10 \
    --block_size 512 \
    --do_train \
    --evaluation_strategy "no" \
    --validation_split_percentage 0 \
    --fp16 True \
    --fp16_full_eval True \
    --streaming \
    --ddp_timeout 3600 \
    --seed 1 \
    --gradient_checkpointing False \
    --output_dir <set output directory path here> \
    --save_steps 10 \
    --save_total_limit 2 \
    --video_conv_kernel 6 \
    --video_conv_stride 2 \
    --audio_conv_kernel 12 \
    --audio_conv_stride 9 \
    --is_cross_attn False \
    --is_simple_prompt False \
    --is_audio_modality True \
    --use_macaw_ckpt False \
    --is_ot False \
    # --resume_from_checkpoint <set ckpt path here> 

