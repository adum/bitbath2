// Standard headers used by the tournament source, in place of GCC's bits/stdc++.h.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <queue>
#include <set>
#include <sstream>
#include <string>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>
#include <emscripten.h>

// A move can reveal units. Give the original parser every SEE line before the
// final order reply, so Astra can replan immediately, just as in the tournament.
EM_JS(char*, browser_order, (const char* command), {
    return stringToNewUTF8(Module['sendOrder'](UTF8ToString(command)));
});
EM_JS_DEPS(order_strings, "$stringToNewUTF8,$UTF8ToString");

static std::istringstream browserReply(const std::string& order) {
    char* reply = browser_order(order.c_str());
    std::istringstream input(reply);
    std::free(reply);
    return input;
}
