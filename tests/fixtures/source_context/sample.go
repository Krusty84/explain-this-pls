package sample
import "fmt"
type Box struct { value int }
func run() {
  fmt.Println("hi")
}
func (box *Box) Method() {}
