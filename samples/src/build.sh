#!/bin/sh
# 重新构建 samples/ 下的 ELF 样例（需要 gcc）。输出目录是本脚本所在目录的上一级。
set -eu
SRC=$(cd "$(dirname "$0")" && pwd)
OUT=$(dirname "$SRC")

mkdir -p "$OUT/vendor_alpha" "$OUT/vendor_beta"

# 两份同名私有库 libfoo.so.1：SONAME 与文件名一致
gcc -shared -fPIC -O2 -Wl,--soname,libfoo.so.1 \
    -Wl,--version-script="$SRC/foo_v1.map" \
    -o "$OUT/vendor_alpha/libfoo.so.1" "$SRC/foo_v1.c"

gcc -shared -fPIC -O2 -Wl,--soname,libfoo.so.1 \
    -Wl,--version-script="$SRC/foo_v2.map" \
    -o "$OUT/vendor_beta/libfoo.so.1" "$SRC/foo_v2.c"

# 插件：DT_RUNPATH=$ORIGIN/<各自私有库目录>，DT_NEEDED 记录 SONAME
gcc -shared -fPIC -O2 -Wl,-rpath,'$ORIGIN/vendor_alpha' \
    -o "$OUT/plugin_alpha.so" "$SRC/plugin_alpha.c" \
    "$OUT/vendor_alpha/libfoo.so.1"

gcc -shared -fPIC -O2 -Wl,-rpath,'$ORIGIN/vendor_beta' \
    -o "$OUT/plugin_beta.so" "$SRC/plugin_beta.c" \
    "$OUT/vendor_beta/libfoo.so.1"

echo "built under $OUT"
