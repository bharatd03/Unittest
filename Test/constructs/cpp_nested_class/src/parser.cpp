// parser.cpp
#include "parser.hpp"
int Parser::Token::kind() const {
    if (c_ >= '0' && c_ <= '9') {
        return 1;
    }
    if ((c_ >= 'a' && c_ <= 'z') || (c_ >= 'A' && c_ <= 'Z')) {
        return 2;
    }
    return 0;
}
