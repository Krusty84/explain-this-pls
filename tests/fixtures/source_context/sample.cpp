#include <string>
#define VALUE 3
namespace sample {
class Box {
 public:
  Box();
  int method(
    int input
  );
};
int Box::method(int input) { return input; }
}
