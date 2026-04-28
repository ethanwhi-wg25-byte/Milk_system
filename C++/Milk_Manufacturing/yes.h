#ifndef YES_H
#define YES_H

#include <iostream>
#include <iomanip>
#include <thread>
#include <chrono>

using namespace std;

class MilkProcessor {
private:
    double total_num_bottle;
    double margin;
    double total_cost;
    double total_revenue;
    double total_profit;
    
    const double ton_to_liter = 1000.0;
    const double bottle_volume = 0.5;
    const double wholesale_price = 4.80;
    const double cost_per_bottle = 3.20;

public:
    MilkProcessor() {}
    
    void processRawMaterial(double x_ton) {
        cout << "Analyzing ";
        for (int i = 1; i <= 3; i++) {
            cout << "#" << flush;
            this_thread::sleep_for(chrono::seconds(1));
        }
        cout << endl;
        
        // Calculations
        total_num_bottle = (x_ton * ton_to_liter) / bottle_volume;
        margin = wholesale_price - cost_per_bottle;
        total_cost = total_num_bottle * cost_per_bottle;
        total_revenue = total_num_bottle * wholesale_price;
        total_profit = total_num_bottle * margin;
        
        // Display results
        displayResults();
    }
    
private:
    void displayResults() {
        cout << fixed << setprecision(2) << endl 
             << "+------------------------------+-----------+" << endl 
             << "|" << left << setw(30) << "Item" << "|" << right << setw(11) << "Amount" << "|" << endl
             << "+------------------------------+-----------+" << endl 
             << "|" << left << setw(30) << "Total Bottles" << "|" << right << setw(11) << total_num_bottle << "|" << endl
             << "|" << left << setw(30) << "Total Cost" << "|" << right << setw(11) << total_cost << "|" << endl
             << "|" << left << setw(30) << "Total Revenue" << "|" << right << setw(11) << total_revenue << "|" << endl
             << "|" << left << setw(30) << "Total Profit" << "|" << right << setw(11) << total_profit << "|" << endl 
             << "+------------------------------+-----------+" << endl;
    }
};

#endif
