/* temp.h */
#ifndef TEMP_H
#define TEMP_H
extern int g_temp_raw;
/* @return the temperature in degrees from g_temp_raw */
int temp_read(void);
#endif
