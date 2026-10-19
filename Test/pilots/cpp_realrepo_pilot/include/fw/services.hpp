// services.hpp - framework services in a namespace, taking const pointers to const data.
#ifndef FW_SERVICES_HPP
#define FW_SERVICES_HPP
namespace fw {
void onError(char const * const module, int id);
bool post(Evt const * const e, Signal const limit);
} // namespace fw
#endif
