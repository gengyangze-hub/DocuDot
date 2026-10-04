#!/usr/bin/env bash
# 上传 GitHub 之前跑一遍：确认仓库里没有夹带真实凭证。
#
#   bash scripts/scan_secrets.sh
#
# 退出码 0 = 干净，1 = 发现问题（别 push）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'

# 只扫会被提交的东西：有 git 就按 git 列表，没有就按目录遍历（并排除忽略项）
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  mapfile -t FILES < <(git ls-files)
  SCOPE="git 已跟踪文件（$(printf '%s\n' "${FILES[@]}" | wc -l) 个）"
else
  mapfile -t FILES < <(find . -type f \
    -not -path './.git/*' -not -path './.venv/*' -not -path './venv/*' \
    -not -path './ci_repo/*' -not -path '*/__pycache__/*' \
    -not -path './data/*' -not -name '*.db' -not -name '*.db-wal' -not -name '*.db-shm' \
    -not -name '*.log' -not -name '.env' | sed 's|^\./||')
  SCOPE="目录遍历（$(printf '%s\n' "${FILES[@]}" | wc -l) 个文件）"
fi

echo "扫描范围：$SCOPE"
echo

FOUND=0
report() { printf '%s✗%s %s\n' "$RED" "$OFF" "$1"; FOUND=1; }

# 1) 不该存在的文件本身
for name in ".env" "data" "ci_repo"; do
  if [ -e "$name" ]; then
    case "$name" in
      .env) report "存在 .env（含真实凭证）—— 确认它被 .gitignore 挡住，且不要提交" ;;
      *)    report "存在 $name/ —— 确认它不该进仓库" ;;
    esac
  fi
done

# 2) 各家的密钥样式
scan_pattern() {
  local label="$1" pattern="$2"
  local hits
  hits="$(printf '%s\n' "${FILES[@]}" | xargs -r grep -nEI "$pattern" 2>/dev/null \
          | grep -v '^\.env\.example' | grep -v 'scan_secrets.sh' | head -8)"
  if [ -n "$hits" ]; then
    report "$label"
    printf '%s\n' "$hits" | sed 's/^/     /'
  fi
}

scan_pattern "疑似 OpenAI/DeepSeek 风格密钥（sk-…，20 位以上）" 'sk-[A-Za-z0-9]{20,}'
scan_pattern "疑似 32 位密钥字面量（QQ AppSecret 等）"        '[A-Za-z0-9]{32}'
scan_pattern "疑似填写了 QQ_APP_ID 真值"                      'QQ_APP_ID=[0-9]{6,}'
scan_pattern "疑似填写了 QQ_APP_SECRET 真值"                  'QQ_APP_SECRET=[A-Za-z0-9]{16,}'
scan_pattern "疑似填写了 LLM_API_KEY 真值"                    'LLM_API_KEY=[A-Za-z0-9_-]{16,}'
scan_pattern "疑似填写了 BOOTSTRAP_API_KEY 真值"              'BOOTSTRAP_API_KEY=[A-Za-z0-9_-]{12,}'

echo
if [ "$FOUND" -eq 0 ]; then
  printf '%s✓ 没发现夹带的凭证，可以上传%s\n' "$GREEN" "$OFF"
  exit 0
fi
printf '%s发现疑点，先逐条确认再决定是否上传。%s\n' "$YELLOW" "$OFF"
echo "提示：.env.example 里的空值/占位符是正常的；上面命中的行才是要处理的。"
exit 1
