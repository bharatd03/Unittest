/* klist.c - list functions taking `T * const` parameters (MISRA style). */
#include "kernel.h"
#include "klist.h"

void klist_init(kList_t * const list)
{
    list->count = 0;
    list->index = &list->end;
    list->end.value = 0xFFFFFFFFU;
    list->end.next = &list->end;
    list->end.owner = list;
}

kBase_t klist_insert_end(kList_t * const list, kItem_t * const item)
{
    item->owner = list;
    if ((kTick_t)list->count >= KLIST_MAX) {
        return -1;
    }
    list->count++;
    return list->count;
}

kBase_t klist_count(const kList_t * const list)
{
    if (list->count > 0) {
        return list->count;
    }
    return 0;
}
