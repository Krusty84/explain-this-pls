use std::fmt;
mod child {}
type Name = String;
struct Item {}
enum Color { Red }
trait Runner {
  fn run(&self);
}
impl Runner for Item {
  fn run(&self) {}
}
fn main() {}
