#include <iostream>
using namespace std;

int main(){
    int test_case = 102;
    const double tax = 1.08;
    double total_amount[100] = {0};
    double total_expense[100] = {0};
    double value;

    for (int i = 0; i < test_case; i++){
        total_amount[i] = 100.0;
        value = 10.0;
        total_expense[i] = 0;
        
        while (value != -1){
            total_expense[i] += value;
            value = -1; // simulate the -1 input
        }
        total_expense[i] *= tax;
    }

    cout << "--- 数组内部数据 ---" << endl;
    for (int i = 98; i < test_case; i++){
        cout << "Index " << i 
             << " | total_amount: " << total_amount[i] 
             << " | total_expense: " << total_expense[i] 
             << endl;
    }
    return 0;
}
