#include "decode.hpp"
namespace telemetry {
int decode(Sample data, Encoding kind)
{
    if (kind == Encoding::Scaled && data.value > 8) return 1;
    return 0;
}
}
