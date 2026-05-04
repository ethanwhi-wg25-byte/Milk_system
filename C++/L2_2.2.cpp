#include <iostream>

using namespace std;

int main()
{
// Declare the variables
int i1, i2, sum, difference, product, quotient, remainder;

// Obtain the data from the user
cout << "Input first integer followed by hitting ENTER: ";
cin >> i1;
cout << "Input second integer followed by hitting ENTER: ";
cin >> i2;
// Do the arithmetic
sum = i1 + i2;
difference = i1 - i2;
product = i1 * i2;
quotient = i1 / i2;
remainder = i1 % i2;

// Output the results
cout << endl;
cout << "The sum of " << i1 << " and " << i2 << " is " << sum << endl;
cout << "The difference of " << i1 << " and " << i2 << " is "
<< difference << endl;
cout << "The product of " << i1 << " and " << i2 << " is "
<< product << endl;
cout << "The quotient of " << i1 << " and " << i2 << " is "
<< quotient << endl;
cout << "The remainder when " << i1 << " is divided by " << i2
<< " is " << remainder << endl;
return 0;
}
