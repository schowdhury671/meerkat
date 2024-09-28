export NCCL_DEBUG=INFO
export NCCL_SOCKET_IFNAME=eth1
export NCCL_IB_GID_INDEX=3
export NCCL_IB_SL=3
export NCCL_NET_GDR_READ=1

export CHIEF_IP=127.0.0.2
export MASTER_ADDR="${CHIEF_IP:=localhost}"
export MASTER_PORT="${MASTER_PORT:=29501}"

path=./
train_path=$path/run_clm_llms_inference.py
    
CUDA_VISIBLE_DEVICES=0 python ${train_path} \
    --train_file /data/all_data.cache \
    --model_name_or_path ${path} \
    --dataset_name <set dataset name here> \
    --preprocessing_num_workers 2 \
    --per_device_train_batch_size 6 \
    --per_device_eval_batch_size 2 \
    --gradient_accumulation_steps 2 \
    --num_train_epochs 5 \
    --save_strategy "steps" \
    --save_steps 5000 \
    --save_total_limit 1 \
    --learning_rate 3e-5 \
    --weight_decay 0. \
    --warmup_ratio 0.03 \
    --lr_scheduler_type "cosine" \
    --logging_steps 10 \
    --block_size 512 \
    --do_eval \
    --evaluation_strategy "no" \
    --validation_split_percentage 0 \
    --fp16 True \
    --fp16_full_eval True \
    --streaming \
    --ddp_timeout 3600 \
    --seed 1 \
    --gradient_checkpointing False \
    --output_dir trained_models/MM-LLMs/mm_llms_trainer/ \
    --save_steps 10 \
    --save_total_limit 5 \
    --video_conv_kernel 6 \
    --video_conv_stride 2 \
    --audio_conv_kernel 12 \
    --audio_conv_stride 9 \
    --is_cross_attn False \
    --is_simple_prompt False \
    --is_audio_modality False \
   