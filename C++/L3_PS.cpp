#include <iostream>
#include <iomanip>
#include <cmath>
using namespace std;

int main(){
  // labor charge = $44
  // parts = p
  // hours = h

   const double RATE  = 44;
   double cost, hour, totalcharge, laborcharge;

    cout << left << setw(50) << "Enter the total cost of the parts: ";
    cin >> cost;

    cout << left << setw(50) << "Enter the total hours of labor: ";
    cin >> hour ;

    laborcharge = hour * RATE;
    totalcharge = laborcharge + cost;
    
    cout << "-------------------------------------------" << endl 
        << setw(30) << left << "Cost of parts:" << right << setw(10)  << cost << endl 
        << setw(30) << left << "Total Labor charge:" << right << setw(10)  << laborcharge << endl 
        << setw(30) << left << "Total charge:" <<  right << setw(10)  << totalcharge << endl 
         << "-------------------------------------------" ;
        
    return 0;
}
    
    
