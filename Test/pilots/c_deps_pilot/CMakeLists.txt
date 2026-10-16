cmake_minimum_required(VERSION 3.16)
project(mini C)
add_library(mini src/ctrl.c src/brake_ctrl.c src/steer_assist.c lib/util.c lib/hw.c)
target_include_directories(mini PUBLIC include)
target_compile_definitions(mini PRIVATE MINI_LIMIT=40 BOARD_REV=2)
# A second build variant of ctrl.c with other limits (A3-BD14): listed and analysed, never built for the
# unit tests. Only ctrl.c, so the real dependencies keep the flags of the mini library.
add_library(mini_4ch OBJECT src/ctrl.c)
target_include_directories(mini_4ch PUBLIC include)
target_compile_definitions(mini_4ch PRIVATE MINI_LIMIT=60 BOARD_REV=3)
