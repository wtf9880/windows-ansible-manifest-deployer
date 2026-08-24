#include <iostream>
#include <string_view>

int main(int argc, char* argv[]) {
    const std::string_view who = argc > 1 ? argv[1] : "Windows";
    std::cout << "Hello, " << who << "! Built with Zig for x86_64 Windows.\n";
    return 0;
}

