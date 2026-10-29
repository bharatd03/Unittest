// speed.hpp
#pragma once
#include <cstdint>
namespace car {
struct Wheels {
    std::uint16_t fl;
    std::uint16_t fr;
};
class Estimator {
public:
    // a spread above 20 between the wheels is implausible; after 3 implausible updates in a row the last estimate is held
    std::uint16_t update(const Wheels& wheels);
private:
    std::uint8_t bad_{0};
    std::uint16_t speed_{0};
};
}
