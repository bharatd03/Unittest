// svc_diag.hpp - the diagnosis service the chassis functions report to (injected).
#ifndef SVC_DIAG_HPP
#define SVC_DIAG_HPP

#include <cstdint>

namespace svc {
class IDiagSink {
public:
    virtual ~IDiagSink() = default;
    virtual void report(std::uint16_t code, bool active) = 0;
    virtual std::uint8_t level() const = 0;
};
}  // namespace svc

#endif
