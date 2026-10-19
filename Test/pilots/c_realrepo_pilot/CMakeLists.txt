cmake_minimum_required(VERSION 3.15)
project(realrepo_pilot C)
# Like many firmware projects: the library lists its include folders; codec/ finds its own header next to
# the source, without an include path of its own.
add_library(fw STATIC kernel/src/klist.c kernel/src/kqueue.c codec/ring.c drivers/crc_drv.c)
target_include_directories(fw PRIVATE kernel/include drivers)
