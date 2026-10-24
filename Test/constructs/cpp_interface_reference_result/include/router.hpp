// router.hpp
#pragma once
#include "table.hpp"
class Router {
public:
    explicit Router(const ITable& table) : table_(table) {}
    /// @return the port for dest, -1 when the route is down
    int port_for(int dest) const;
private:
    const ITable& table_;
};
