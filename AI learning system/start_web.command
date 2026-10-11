#!/bin/zsh
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  echo '请先按照 README 创建 .venv 并安装 requirements.txt'
  exit 1
fi
exec .venv/bin/python -m learning_system.web
