/* beta 版 libfoo：同时提供 foo@FOO_1.0（兼容）与 foo@@FOO_2.0（默认） */
int foo_v1_impl(void) { return 1; }
int foo_v2_impl(void) { return 2; }
__asm__(".symver foo_v1_impl,foo@FOO_1.0");
__asm__(".symver foo_v2_impl,foo@@FOO_2.0");
