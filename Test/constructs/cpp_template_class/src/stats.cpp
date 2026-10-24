// stats.cpp
#include "stats.hpp"
#include "minmax.hpp"
int stats_span(int a, int b, int c) {
    MinMax<int> m;
    m.add(a);
    m.add(b);
    m.add(c);
    return m.span();
}
