#!/usr/bin/env bash
# 用 AstrBot 官方 Docker 镜像检查 DeepDeep 的兼容性：
#   1. 在镜像里运行 compat_probe.py，逐项检查依赖的接口
#   2. 挂载插件真正启动一次 AstrBot，确认插件能加载、工具能注册
#
# 用法：scripts/compat_check.sh v4.29.1 [报告文件]
# 退出码：0 通过，1 不兼容，2 跳过（镜像还没发布）
set -uo pipefail

TAG="${1:?用法: compat_check.sh <AstrBot 版本，如 v4.29.1> [报告文件]}"
REPORT="${2:-compat_report.md}"
IMAGE="soulter/astrbot:${TAG}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN="astrbot_plugin_deepdeep"
WORK="$(mktemp -d)"
NAME="deepdeep_compat_$$"

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  # 容器以 root 写入挂载目录，普通用户可能删不掉
  rm -rf "$WORK" 2>/dev/null || sudo rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

{
  echo "## AstrBot ${TAG} 兼容性检查"
  echo
} >"$REPORT"

if ! docker pull -q "$IMAGE" >/dev/null 2>&1; then
  echo "镜像 ${IMAGE} 还不可用，跳过本次检查。" | tee -a "$REPORT"
  exit 2
fi

status=0

# ---------- 1. 接口探针 ----------
echo "### 接口检查" >>"$REPORT"
echo '```text' >>"$REPORT"
if docker run --rm --network none --entrypoint python -w /AstrBot \
  -v "$ROOT/scripts:/probe:ro" "$IMAGE" -W ignore /probe/compat_probe.py >"$WORK/probe.txt" 2>&1; then
  :
else
  status=1
fi
# 导入 AstrBot 时会打印启动日志和第三方库的警告，报告里只保留检查结果
sed -E 's/\x1b\[[0-9;]*m//g' "$WORK/probe.txt" | grep -E '^(✓|✗)|项通过|Traceback|Error' | tee -a "$REPORT"
echo '```' >>"$REPORT"

# ---------- 2. 真实加载 ----------
mkdir -p "$WORK/data/plugins/$PLUGIN"
# 只放发布包里会有的文件（与 git archive 一致）
(cd "$ROOT" && git archive --format=tar HEAD) | tar -x -C "$WORK/data/plugins/$PLUGIN"

docker run -d --name "$NAME" --network none -v "$WORK/data:/AstrBot/data" "$IMAGE" >/dev/null
for _ in $(seq 1 60); do
  if docker logs "$NAME" 2>&1 | grep -qE "WebUI is ready|Plugin ${PLUGIN} \("; then
    break
  fi
  sleep 3
done
sleep 5
docker logs "$NAME" 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' >"$WORK/astrbot.log"

{
  echo
  echo "### 插件加载"
  echo '```text'
} >>"$REPORT"

loaded=$(grep -cE "Plugin ${PLUGIN} \(v" "$WORK/astrbot.log" || true)
tools=$(grep -cE "Added llm tool: deepdeep_(web_search|web_fetch|tavily_search)" "$WORK/astrbot.log" || true)
# 和本插件有关的报错：错误行本身提到插件，或 Traceback 里出现插件路径
errors=$(grep -iE "deepdeep" "$WORK/astrbot.log" | grep -iE "error|exception|traceback|failed|失败" || true)
tb=$(grep -A25 "Traceback" "$WORK/astrbot.log" | grep -B5 -A20 "$PLUGIN" | head -60 || true)

grep -m1 -oE "AstrBot v[0-9][0-9a-z.\-]*" "$WORK/astrbot.log" | tee -a "$REPORT" || true
grep -E "$PLUGIN|deepdeep_web" "$WORK/astrbot.log" | cut -c1-200 | tee -a "$REPORT" || true
if [ "$loaded" -lt 1 ]; then
  echo "✗ 插件没有加载成功" | tee -a "$REPORT"
  status=1
fi
if [ "$tools" -lt 3 ]; then
  echo "✗ 工具没有全部注册（期望 3 个，实际 ${tools} 个）" | tee -a "$REPORT"
  status=1
fi
if [ -n "$errors$tb" ]; then
  echo "✗ 发现与插件有关的报错：" | tee -a "$REPORT"
  printf '%s\n%s\n' "$errors" "$tb" | head -60 | tee -a "$REPORT"
  status=1
fi
echo '```' >>"$REPORT"

if [ "$status" -eq 0 ]; then
  echo -e "\n✅ 通过" | tee -a "$REPORT"
else
  echo -e "\n❌ 不兼容" | tee -a "$REPORT"
fi
exit "$status"
