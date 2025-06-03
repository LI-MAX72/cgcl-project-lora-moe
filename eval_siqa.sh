export CUDA_VISIBLE_DEVICES=0
python evaluation_scienceqa.py \
         --test_dataset "/home/sub6-zt/caonan/dataset/siqa_test_all.json" \
         --base_model "/home/sub6-zt/caonan/model/Llama-3.1-8B" \
         --mola_weights "/home/sub6-zt/caonan/MoLA_l3_last/results/qa_siqa_mola-1ep-2" \
         --batch_size 16 \
         --lora_target_modules "q_proj,v_proj,k_proj,o_proj,gate_proj,down_proj,up_proj" \
         --number_experts "2,2,2,2,2,2,2,2,4,4,4,4,4,4,4,4,6,6,6,6,6,6,6,6,8,8,8,8,8,8,8,8" \
         --top_k "2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2" \
         --save_path "./eval/qa_siqa_mola-1ep-2.json"