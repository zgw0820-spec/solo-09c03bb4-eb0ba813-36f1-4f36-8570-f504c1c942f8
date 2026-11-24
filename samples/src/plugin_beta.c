/* 插件 beta：依赖 libfoo.so.1 的 foo@FOO_2.0，并引用 libc（演示系统库跳过） */
#include <string.h>
extern int foo(void);
int beta_main(const char *s) { return foo() * 10 + (int)strlen(s); }
