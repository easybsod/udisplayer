// SPDX-FileCopyrightText: 2026 udisplayer contributors
// SPDX-License-Identifier: MIT
#include "common.h"
#include <cassert>
#include <cstring>
int main() {
    davs2_mgr_t a = {}, b = {};
    a.skip_nonref_enabled = b.skip_nonref_enabled = 1;
    a.outpics.output = 11;
    b.outpics.output = 11;
    a.skipped_pocs[0] = 9;  // stale
    a.skipped_pocs[1] = 11;
    a.skipped_pocs[2] = 13;
    a.skipped_poc_count = 3;
    b.skipped_pocs[0] = 12;
    b.skipped_poc_count = 1;
    davs2_skip_advance_output_locked(&a);
    assert(a.outpics.output == 12);
    assert(a.skipped_poc_count == 1 && a.skipped_pocs[0] == 13);
    assert(b.outpics.output == 11 && b.skipped_poc_count == 1);
    davs2_skip_advance_output_locked(&b);
    assert(b.outpics.output == 11);
    b.outpics.output = 12;
    davs2_skip_advance_output_locked(&b);
    assert(b.outpics.output == 13 && b.skipped_poc_count == 0);
    a.outpics.output = 13;
    davs2_skip_advance_output_locked(&a);
    assert(a.outpics.output == 14 && a.skipped_poc_count == 0);
    // The disabled decoder does not consult even a synthetic registry.
    a.skip_nonref_enabled = 0;
    a.skipped_pocs[0] = 14;
    a.skipped_poc_count = 1;
    davs2_skip_advance_output_locked(&a);
    assert(a.outpics.output == 14 && a.skipped_poc_count == 1);
    return 0;
}
