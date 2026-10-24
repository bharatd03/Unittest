// filter.cpp
#include "filter.hpp"
namespace {
class Window {
public:
    int add(int sample) {
        int avg = (last_ + sample) / 2;
        last_ = sample;
        if (avg > 100) {
            return 100;
        }
        return avg;
    }
private:
    int last_ = 0;
};
Window g_window;
}  // namespace
int filter_add(int sample) {
    return g_window.add(sample);
}
