import { getAppRoot } from "onload/loadConfig";
import axios from "axios";
import { getGalaxyInstance } from "app";

export function getRecentInvocations() {
    const Galaxy = getGalaxyInstance();
    const params = { user_id: Galaxy.user.id, limit: 150 };
    const url = `${getAppRoot()}api/invocations`;
    return axios.get(url, { params: params });
}
