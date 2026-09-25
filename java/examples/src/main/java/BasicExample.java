import studio.aight.sdk.Remote;
import studio.aight.sdk.Tracing;

public class BasicExample {

    static void callLlm() {
        Tracing.tracedLlmCall("gpt-4o-mini", 180, 60);
    }

    public static void main(String[] args) throws Exception {
        callLlm();
        callLlm();

        System.out.println(Tracing.COST_PROCESSOR.report());

        if (System.getenv("AIGHT_API_KEY") != null) {
            Remote.push(Tracing.COST_PROCESSOR, null, null);
            System.out.println("pushed to aight.studio");
        }
    }
}
