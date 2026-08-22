#!/bin/bash
# Quick functionality test for multiple GGUFs

LLAMA_CLI="/storage/THUDM_GLM-4-32B-0414-GGUF/llama.cpp/build/bin/llama-cli"
PROMPT_FILE="/tmp/summarize_test.txt"
RESULTS_DIR="/storage/jmfts_data/gguf_tests"
mkdir -p "$RESULTS_DIR"

# Create test prompt
cat > "$PROMPT_FILE" << 'EOF'
Summarize this news article in one sentence:

Prison Link Cymru had 1,099 referrals in 2015-16 and said some ex-offenders were living rough for up to a year before finding suitable accommodation. Workers at the charity claim investment in housing would be cheaper than jailing homeless repeat offenders. The Welsh Government said more people than ever were getting help to address housing problems.

Summary:
EOF

# Models to test (path, name, context_size)
declare -a MODELS=(
    "/fast/Qwen3-32B-Q4_K_M.gguf|Qwen3-32B|2048"
    "/storage/THUDM_GLM-4-32B-0414-GGUF/THUDM_GLM-4-32B-0414-Q4_K_M.gguf|GLM4-32B|2048"
    "/fast/google_gemma-3-4b-it-Q8_0.gguf|Gemma3-4B|4096"
    "/fast/model/Phi-3.5-mini-instruct-Q6_K_L.gguf|Phi3.5-mini|4096"
    "/fast/model/qwen2.5-3b-instruct-q6_k.gguf|Qwen2.5-3B|4096"
    "/fast/model/Qwen_Qwen2.5-VL-32B-Instruct-Q4_K_S.gguf|Qwen2.5-VL-32B|2048"
)

echo "=========================================="
echo "GGUF Model Functionality Tests"
echo "=========================================="
echo ""

for model_spec in "${MODELS[@]}"; do
    IFS='|' read -r model_path model_name ctx_size <<< "$model_spec"

    echo "Testing: $model_name"
    echo "  Path: $model_path"

    if [ ! -f "$model_path" ]; then
        echo "  ERROR: Model file not found!"
        echo ""
        continue
    fi

    output_file="$RESULTS_DIR/${model_name}.txt"

    # Run inference with timeout
    start_time=$(date +%s.%N)

    timeout 120 "$LLAMA_CLI" \
        -m "$model_path" \
        -f "$PROMPT_FILE" \
        -n 100 \
        -ngl 99 \
        -c "$ctx_size" \
        --temp 0.3 \
        --no-display-prompt \
        -no-cnv \
        2>&1 | tee "$output_file"

    exit_code=$?
    end_time=$(date +%s.%N)
    elapsed=$(echo "$end_time - $start_time" | bc)

    echo ""
    echo "  Exit code: $exit_code"
    echo "  Time: ${elapsed}s"
    echo "  Output saved: $output_file"
    echo "------------------------------------------"
    echo ""

    # Brief pause to ensure GPU memory is freed
    sleep 2
done

echo "All tests complete!"
echo "Results in: $RESULTS_DIR"
