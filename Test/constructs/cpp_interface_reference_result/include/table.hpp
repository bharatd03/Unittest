// table.hpp
#pragma once
struct Route {
    int port = 0;
    bool up = false;
};
class ITable {
public:
    virtual ~ITable() = default;
    virtual const Route& lookup(int dest) const = 0;
};
