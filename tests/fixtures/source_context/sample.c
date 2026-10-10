#include <stdio.h>
#define PLUS(x) \
  ((x) + 1)
typedef unsigned Count;
struct Box { int value; };
int run(
  int input
);
int run(int input) { return PLUS(input); }
