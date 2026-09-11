class Counter {
public:
    int add(int value) const {
        return value + 1;
    }
};

int main() {
    Counter counter;
    return counter.add(2);
}
