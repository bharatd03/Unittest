/* Pilot SUT (C, ASIL D steering): electric power steering assist torque. */
#ifndef STEER_ASSIST_H
#define STEER_ASSIST_H

#define STEER_POINTS    4
#define STEER_MAX       300   /* assist torque limit, 0.01 Nm */
#define STEER_RATE      50    /* largest change per call */
#define STEER_HANDS_ON  20    /* driver torque that counts as hands on the wheel */
#define STEER_LANE_NUDGE 15

/** STR-1: assist gain in percent for a vehicle speed, interpolated over the gain table.
 *  @return 100 at standstill down to 20 at 120 km/h and above */
int steer_gain(int speed_kph);

/** STR-2 .. STR-4: assist torque for the driver torque, limited to +-STEER_MAX and to STEER_RATE per call.
 *  @return the assist torque */
int steer_assist_torque(int driver_torque, int speed_kph, int lane_assist);

/** STR-5: the rate limiter starts again from 0. */
void steer_reset(void);

#endif
