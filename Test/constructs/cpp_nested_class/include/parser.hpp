// parser.hpp
#pragma once
class Parser {
public:
    class Token {
    public:
        explicit Token(char c) : c_(c) {}
        /// @return 1 for a digit, 2 for a letter, 0 otherwise
        int kind() const;
    private:
        char c_;
    };
};
