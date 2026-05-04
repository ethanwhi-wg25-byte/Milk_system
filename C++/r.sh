#!/bin/bash
# Simple C++ runner script
# Usage: ./r.sh <cpp-file>

if [ -z "$1" ]; then
    echo "Usage: ./r.sh <cpp-file>"
    exit 1
fi

# Allow file paths with spaces even when user forgets quotes.
FILE_PATH="$*"

echo "Compiling $FILE_PATH..."
# Check if file has C++ extension
if [[ "$FILE_PATH" == *.cpp || "$FILE_PATH" == *.cc || "$FILE_PATH" == *.cxx ]]; then
    # File has C++ extension, compile normally
    clang++ "$FILE_PATH" -o /tmp/cpp_temp_exec
else
    # File doesn't have extension or unknown extension, treat as C++
    clang++ -x c++ "$FILE_PATH" -o /tmp/cpp_temp_exec
fi

if [ $? -eq 0 ]; then
    echo "Compilation successful. Running..."
    /tmp/cpp_temp_exec
else
    echo "Compilation failed!"
    exit 1
fi
