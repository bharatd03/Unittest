// rx.hpp
#pragma once
// @return -1 without a frame, -2 for another id, -3 for a length other than 8, else the first data byte
int receive();
