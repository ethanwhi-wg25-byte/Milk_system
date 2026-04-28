#include <iostream>
#include <string>

using namespace std;

int main(){
    const int MAX = 5;
    int score[MAX];
    string name[MAX];

    cout << "Enter scores of the math test" << endl;

    for(int i = 0 ; i < MAX ; i ++){
        cout << "Student " << i + 1 << endl;
        cout << "Enter name: ";
        cin >> name[i];
        
        cout << "Enter your score: ";
        cin >> score[i];
    }
    

    return 0;
}
