/* klist.h - needs kernel.h first, like FreeRTOS list.h. */
#ifndef KLIST_H
#define KLIST_H
#ifndef KERNEL_H
#error "kernel.h must be included before klist.h"
#endif
struct kList;
typedef struct kItem {
    kTick_t value;
    struct kItem *next;
    struct kList *owner;
} kItem_t;
typedef struct kList {
    kBase_t count;
    kItem_t *index;
    kItem_t end;
} kList_t;
void klist_init(kList_t * const list);
kBase_t klist_insert_end(kList_t * const list, kItem_t * const item);
kBase_t klist_count(const kList_t * const list);
#endif
