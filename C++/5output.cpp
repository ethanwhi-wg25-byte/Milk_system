#include <iostream>
using namespace std;

int main(){
    
    int N1, N2,sum ,difference ,product ,quotient , remainder;
    cout <<"Input first integer followed by hitting ENTER:";
    cin >> N1;
    
    cout <<"Input seconf integer followed by hitting ENTER";
    cin >> N2;
    
    sum = N1 +  N2;
    difference = N1 - N2;
    product = N1 * N2;
    quotient = N1 / N2;
    remainder = N1 % N2;
    
    cout << "The sum of " << N1 << " and " << N2 <<" = " << sum 
         << "\nThe difference of " << N1 << " and " << N2 <<" = " << difference 
         << "\nThe product of " << N1 << " and " << N2 <<" = " << product
         << "\nThe quotient of " << N1 << " and " << N2 <<" = " << quotient
         << "\nThe remainder of " << N1 << " and " << N2 <<" = " << remainder;
}