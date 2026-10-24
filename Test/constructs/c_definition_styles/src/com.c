/* com.c */
#include "com.h"
FUNC(int, COM_CODE) Com_Clamp(int value)
{
    if (value > 100) {
        return 100;
    }
    return value;
}
__attribute__((weak)) int Com_Handler(void)
{
    return 1;
}
#if COM_WIDE
int Com_Scale(int v) {
#else
int Com_Scale(int v)
{
#endif
    return v * 2;
}
static int
com_internal(int x)
{
    return x + Com_Scale(x);
}
