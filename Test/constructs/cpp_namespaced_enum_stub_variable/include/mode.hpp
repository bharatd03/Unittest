// mode.hpp
#pragma once
namespace app {
enum class Mode { Off, Service, Run };
/// @return 2 in service, -1 for a low supply, -2 when too hot, else 0
int supervise(Mode mode);
}
