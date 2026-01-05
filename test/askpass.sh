#!/usr/bin/env sh

# ssh 会把提示文本作为第一个参数传进来
echo "ASKPASS CALLED with prompt: $1" >&2

# === 测试用密码（替换成你真实测试机器的密码）===
echo "YOUR_PASSWORD_HERE"
