#include <iostream>

using namespace std;

int main(){

    double num1, num2, answer ; 
    char Operator;

    cout << "Enter num 1 : " ;
    cin >> num1;

    cout << "Enter operator: ";
    cin >> Operator;

    cout << "Enter num 2 : " ;
    cin >> num2;

    switch (Operator) {
        case '+' :
        answer = num1 + num2;
        cout << answer;
            break;
        
        case '-' :
        answer = num1 - num2;
        cout << answer;
            break;

        case '*' :
        answer = num1 * num2;
        cout << answer;
            break;

        case '/' :
        if (num2 == 0 )
            cout << "Result: Error";
            else {
                answer = num1 / num2;
                cout << answer;
            }
            break;
            
        default :
                cout << "Result: Error";
            }
    }
