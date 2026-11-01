#pragma once
namespace telemetry {
enum class Encoding { Raw, Scaled };
struct Sample { int value; };
int decode(Sample data, Encoding kind);
}
