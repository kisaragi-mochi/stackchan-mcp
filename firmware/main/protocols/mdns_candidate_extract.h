#ifndef _MDNS_CANDIDATE_EXTRACT_H_
#define _MDNS_CANDIDATE_EXTRACT_H_

#include "mdns_gateway_discovery.h"

#include <mdns.h>

#include <vector>

namespace stackchan::mdns {

struct ExtractedGatewayCandidates {
    int accepted_instances = 0;
    std::vector<MdnsGatewayCandidate> candidates;
};

int CountResults(const mdns_result_t* results);

ExtractedGatewayCandidates ExtractGatewayCandidatesFromMdnsResults(const mdns_result_t* results,
                                                                   int result_count);

}  // namespace stackchan::mdns

#endif  // _MDNS_CANDIDATE_EXTRACT_H_
