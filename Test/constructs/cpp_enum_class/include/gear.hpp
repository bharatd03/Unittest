// gear.hpp
#pragma once
enum class Gear { Park, Reverse, Drive };
/// @return the speed limit of the gear
int gear_limit(Gear g);
