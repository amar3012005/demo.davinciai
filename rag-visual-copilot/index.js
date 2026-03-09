// index.js
import { Antigravity } from '@google/antigravity';

const agent = new Antigravity({ config: './agent.yaml' });

agent.on('task', async (task) => {
    const desc = task.description.toLowerCase();

    // 1. If it involves mapping new websites or logic trees
    if (desc.includes('index') || desc.includes('map') || desc.includes('hierarchy')) {
        return await agent.delegate('SiteIndexer', task);
    }

    // 2. If it involves fixing crashes or variable state
    if (desc.includes('error') || desc.includes('crash') || desc.includes('target_node')) {
        return await agent.delegate('BugHunter', task);
    }

    // 3. If it involves code verification or 'complete_mission' logic
    if (desc.includes('verify') || desc.includes('critic') || desc.includes('entity anchor')) {
        return await agent.delegate('LogicCritic', task);
    }

    // 4. Default to the Pipeliner for general feature development
    return await agent.delegate('ActionPipeliner', task);
});
