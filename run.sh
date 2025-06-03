export CUDA_VISIBLE_DEVICES=1
python mola_training.py \
         --base_model "/home/sub6-zt/caonan/model/Llama-3.1-8B" \
         --data_path "/home/sub6-zt/caonan/code/MoLA_new/scienceqa/science_qa.hf" \
         --output_dir "./scienceqa_mola" \
         --batch_size 16 \
         --micro_batch_size 2 \
         --num_epochs 1 \
         --learning_rate 3e-4 \
         --cutoff_len 256 \
         --val_set_size 1 \
         --lora_r "8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8" \
         --lora_alpha 16 \
         --lora_dropout 0.05 \
         --lora_target_modules "q_proj,v_proj,k_proj,o_proj,gate_proj,down_proj,up_proj" \
         --number_experts "2,2,2,2,2,2,2,2,4,4,4,4,4,4,4,4,6,6,6,6,6,6,6,6,8,8,8,8,8,8,8,8" \
         --top_k "2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2" \
         --train_on_inputs \
         --group_by_length \
         --add_eos_token 