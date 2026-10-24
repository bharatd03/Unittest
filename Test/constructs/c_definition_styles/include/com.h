/* com.h - AUTOSAR-like function macros and attributes */
#ifndef COM_H
#define COM_H
#define FUNC(rettype, memclass) rettype
#define COM_CODE
#ifndef COM_WIDE
#define COM_WIDE 0
#endif
/* @return the clamped value */
FUNC(int, COM_CODE) Com_Clamp(int value);
/* @return 1 when the handler ran */
int Com_Handler(void);
/* @return the scaled value */
int Com_Scale(int v);
#endif
