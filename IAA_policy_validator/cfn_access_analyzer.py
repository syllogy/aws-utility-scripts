import boto3
import json
import os
from cfn_flip import to_json
import argparse
import retry
import botocore
import logging
from dotenv import load_dotenv

#Global boto3 clients
access_client = boto3.client('accessanalyzer')
sts_client = boto3.client('sts')

load_dotenv()

logging.basicConfig(level=os.environ.get("LOGLEVEL", "ERROR"))
Logger = logging.getLogger("cfn_policy_validator")

'''
Argument parser
Returns command line parsed arguments
'''
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", action='store', type=file_path, required=True, help="Path to the cfn template to analyse")
    parser.add_argument('--output', action='store', type=str, required=False, choices=['print', 'file'], help= 'Toggle printing output to cli, or writing to a file')
    parser.add_argument('--ignore_finding_types', action='store', required=False, nargs='+')
    return parser.parse_args()


'''
File Path validator
Validates the file path provided via the "--path" command line variable
'''
def file_path(string):
    if os.path.isfile(string):
        return string
    else:
        Logger.error('File does not exist at perscribed path')
        raise FileNotFoundError(string)


'''
Cloudformation Parser
Accepts an array of policy documents and contextual variables, and parses the document
to filter out the intrinsic functions to provide their computed values
'''
def parse_cfn(policy_array, account_id, region):
    for policy in policy_array:
        for x in policy['PolicyDocument']['Statement']:
            if type(x['Resource']) == list:
                for i, rsc in enumerate(x['Resource']):
                    if type(rsc) == dict and list(rsc.keys())[0] == 'Fn::Sub':
                        x['Resource'][i] = list(rsc.values())[0].replace("${AWS::AccountId}", account_id)
                        x['Resource'][i] = x['Resource'][i].replace("${AWS::Region}", region)
            
            elif type(x['Resource']) == dict and list(x['Resource'].keys())[0] == 'Fn::Sub':
                x['Resource'] = list(x['Resource'].values())[0].replace("${AWS::AccountId}", account_id)
                x['Resource'] = x['Resource'].replace("${AWS::Region}", region)

            ##Conditions
            if x.get('Condition', '') != '': 
                for i, block in enumerate(x['Condition']): 
                    for p, cond in enumerate(x['Condition'][block].keys()):
                        try:
                            if type(x['Condition'][block][cond]) == list:
                                for k, statement in enumerate(x['Condition'][block][cond]):
                                    if type(statement) == dict and list(statement.keys())[0] == 'Fn::Sub':
                                        x['Condition'][block][cond][k] = list(statement.values())[0].replace("${AWS::AccountId}", account_id)
                                        x['Condition'][block][cond][k] = x['Condition'][block][cond][k].replace("${AWS::Region}", region)
                            
                            # To DO: Implement parameter support
                            # elif type(list(x['Condition'][block][cond].keys())[0])== str and list(x['Condition'][block][cond].keys())[0] == 'Ref':
                            #  x[??ondition'][block][cond] == variable

                            elif type(list(x['Condition'][block][cond].keys())[0])== str and list(x['Condition'][block][cond].keys())[0] == 'Fn::Sub':
                                x['Condition'][block][cond] == list(x['Condition'][block][cond].values())[0].replace("${AWS::AccountId}", account_id)
                                x['Condition'][block][cond] == x['Condition'][block][cond].replace("${AWS::Region}", region)
                        
                        except AttributeError:
                            Logger.info('Error caught - condition does not require parsing')
        return policy_array


'''
Policy Validator
Iterates through the policy array, executing the IAA validate policy call,
returning all findings that are not listed as ignored findings
'''
def validate_policy(policy_array, ignored_findings):
    results = {}
    for policy in policy_array:
        print(f'---Analysing: {policy["PolicyName"]}---')
        try:
            response = retry.api.retry_call(
                access_client.validate_policy,
                fkwargs={"locale": 'EN', "policyDocument": json.dumps(policy['PolicyDocument']), 'policyType': 'IDENTITY_POLICY'},
                jitter=5,
                tries=5,
                backoff=1.5,
                max_delay=60,
                exceptions=botocore.exceptions.ClientError
            )
            findings = response['findings']
            if len(findings) > 0:
                results.update({
                    policy["PolicyName"]: ''
                })
                findings_array = []
                for finding in findings:
                    if finding['findingType'] not in ignored_findings:
                        findings_array.append({
                            "Finding Code": f"{finding['issueCode']} ({finding['findingType']})",
                            "Finding Details": finding['findingDetails'],
                            "Location": f"{finding['locations'][0]['path'][0]['value']} #{finding['locations'][0]['path'][1]['index']}, Segment '{finding['locations'][0]['path'][2]['value']}'",
                            "Learn more link": finding['learnMoreLink']
                        })
                
                results.update({
                    policy["PolicyName"]: findings_array
                })
                
            else:
                Logger.info(f"No findings found for {policy['PolicyName']}")
        
        except access_client.exceptions.InternalServerException: 
            Logger.error(f"Failed to validate {policy['PolicyName']} due to Internal Server Error")
            print('\n')
            next
        except:
            Logger.error(f"Failed to validate {policy['PolicyName']}")
            next
    return results


if __name__ == '__main__':
    args = parse_args()
    account_id = sts_client.get_caller_identity()['Account']
    region = os.getenv("REGION")

    path = args.path
    output = args.output
    if args.ignore_finding_types == None: 
        ignored_findings = []
    else:
        ignored_findings = args.ignore_finding_types
    print('\n')
    print('Executing with the following Parameters: ')
    print(f"Cloudformation path: {str(path)}")
    print(f"Region: {str(region)}")
    print(f"Account ID: {str(account_id)}")
    print(f"Output Type: {str(output)}")
    print(f"Ignored Finding Types: {str(ignored_findings)}")
    print('\n')

    try:
        extension = path.split('.')[-1]
        with open(path, 'r') as document: 
            if extension == 'yaml' or extension == 'yml':
                Logger.debug('YAML/YML cloudformation file detected')
                cfn_template = json.loads(to_json(document.read()))
                Resources = cfn_template['Resources']
            else:
                Logger.debug('JSON cloudformation file detected')
                cfn_template = json.loads(document.read())
                Resources = cfn_template['Resources']
    except FileNotFoundError as e:
        Logger.error(f"Error when trying to locate path to Cloudformation file.  Check Path and try again.")
        raise(e)

    # Customer managed policies
    policy_array = [{'PolicyName': policy, 'PolicyDocument': Resources[policy]['Properties']['PolicyDocument']} for policy
        in Resources.keys() if Resources[policy]['Type'] == 'AWS::IAM::ManagedPolicy']

    for policy in Resources.keys():
        if Resources[policy]['Type'] == 'AWS::IAM::Role':
            # Accounting for Roles that only have managed policies attached
            if Resources[policy]['Properties'].get('Policies') != None: 
                for Policy in Resources[policy]['Properties']['Policies']:
                    policy_array.append({
                        'PolicyName': Policy['PolicyName'],
                        'PolicyDocument': Policy['PolicyDocument'],
                    })
        
    parse_policy_array = parse_cfn(policy_array, account_id, region)
    
    Logger.debug('Validating Policies...')
    results = validate_policy(policy_array, ignored_findings)
    # Optional cmd line parameter for output.
    if output == 'print':
        print('---Findings---')
        Logger.info(json.dumps(results, indent=4))
    elif output == 'file':
        if not os.path.isdir('output'):
            Logger.debug("output folder doesn't exist... Creating")
            os.mkdir('output')

        with open('output/results.json', 'w') as doc: 
            json.dump(results, doc, indent=4)
            Logger.debug("results dumped to output/results.json file")

    #Fail if 'results' contains any entries
    #Policy value may be a blank array if all FindingTypes returned are present in ignore_finding_types param
    print('\n')
    for policy_findings in results:
        try:  
            assert len(results[policy_findings]) == 0
        except AssertionError as e:
            print(f"IAA policy validator raised {len(results[policy_findings])} finding(s)")
            print(f"Findings: {json.dumps(results[policy_findings], indent=4)}")
            raise(e)