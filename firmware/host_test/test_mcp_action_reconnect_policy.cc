#include "mcp_action_reconnect_policy.h"

#include <gtest/gtest.h>

TEST(McpActionReconnectPolicyTest, DoublesUntilSixtySecondCap) {
    McpActionReconnectPolicy policy;

    EXPECT_EQ(policy.ConsumeDelayMs(), 5000u);
    EXPECT_EQ(policy.ConsumeDelayMs(), 10000u);
    EXPECT_EQ(policy.ConsumeDelayMs(), 20000u);
    EXPECT_EQ(policy.ConsumeDelayMs(), 40000u);
    EXPECT_EQ(policy.ConsumeDelayMs(), 60000u);
    EXPECT_EQ(policy.ConsumeDelayMs(), 60000u);
    EXPECT_EQ(policy.PeekDelayMs(), 60000u);
}

TEST(McpActionReconnectPolicyTest, SuccessfulHelloResetsBackoff) {
    McpActionReconnectPolicy policy;
    policy.ConsumeDelayMs();
    policy.ConsumeDelayMs();

    policy.Reset();

    EXPECT_EQ(policy.PeekDelayMs(), 5000u);
    EXPECT_EQ(policy.ConsumeDelayMs(), 5000u);
}
