/* 插件 alpha：依赖 libfoo.so.1 的 foo@FOO_1.0，并引用 libc（演示系统库跳过） */
#include <string.h>
extern int foo(void);
int alpha_main(const char *s) { return foo() + (int)strlen(s); }
