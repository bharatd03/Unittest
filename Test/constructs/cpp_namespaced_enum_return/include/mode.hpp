// mode.hpp
#pragma once
namespace car {
enum class Mode { Comfort, Sport, Service };
// a long press selects Service only at standstill; a short press switches Comfort and Sport
Mode select_mode(Mode current, unsigned button, bool stopped);
}
