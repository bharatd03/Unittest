/* Pilot SUT (C, ASIL D braking): brake state machine and pressure request. */
#ifndef BRAKE_CTRL_H
#define BRAKE_CTRL_H

#define BRK_STATUS_HOLD  0x1u   /* hill-hold requested */
#define BRK_STATUS_FAULT 0x2u   /* fault reported by the brake ECU */
#define BRK_MAX_BAR      150

typedef enum { BRK_IDLE, BRK_ARMED, BRK_ACTIVE, BRK_FAULT } brake_state_t;

typedef struct {
    int pedal_pct;      /* 0..100 */
    int speed_kph;
    unsigned status;    /* BRK_STATUS_* bits */
} brake_input_t;

/** BRK-1, BRK-2: the next state of the brake controller.
 *  @return BRK_FAULT for a NULL input or a reported fault, else the next state */
brake_state_t brake_step(brake_state_t state, const brake_input_t *in);

/** BRK-3, BRK-4: brake pressure request in bar from the pedal and two pressure sensors.
 *  @return 0..BRK_MAX_BAR, or -1 for a pedal value outside 0..100 or implausible sensors */
int brake_pressure_request(int pedal_pct);

/** BRK-5: @return the number of faults seen since start-up */
int brake_fault_count(void);

#endif
