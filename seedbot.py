import sys
import logging
import struct
import time
import requests
import random
import threading
import queue
import yaml
import urllib3
import npyscreen
from const import Const
import webhandler
import friend_functions

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timedelta

sys.path.append("./NintendoClients")
from nintendo.nex import nintendo_notification
logname = "error_" + datetime.now().strftime('%Y%m%d_%H%M%S') + ".log"
#logging.basicConfig(level=logging.WARN)
logging.basicConfig(filename=logname,filemode='w',format='%(asctime)s %(message)s',level=logging.INFO)
class cSettings(object):
    def __init__(self,pid,lfcs):
        self.UI = False
        self.version = 0x200
        self.active=1
        self.friendcode = friend_functions.PID2FC(pid)
        self.pid = pid
        self.lfcs=lfcs
        self.BotterCount=0
        self.ServerErrorCount=0
        self.ReconnectNintendo=False
        self.StartTime = datetime.utcnow()
        self.RunTime = str(datetime.utcnow() - self.StartTime).split(".")[0]
        self.Running = True
        self.LastGameChange = datetime.utcnow()
        self.CurrentGame = 0x0004000000131200
        self.PauseUntil = datetime.utcnow()
        self.WaitForFriending = datetime.utcnow()
        self.WaitForResync = datetime.utcnow()
    def UpdateRunTime(self):
        self.RunTime = str(datetime.utcnow() - self.StartTime).split(".")[0]


class Intervals(Const):
    get_friends=5
    error_wait = 10
    harderror_wait = 900
    nintendo_wait = 1200
    friend_timeout = 600
    resync = 30
    change_game = 700
    between_actions = 0.2
    betweenNintendoActions = 0.5
identity_path = "identity.yaml"
if len(sys.argv) >= 2:
    identity_path = sys.argv[1]
identity = yaml.load(open(identity_path, 'r'))


###################################### VARIABLES AND SHIT

RunSettings = cSettings(identity['user_id'],identity['lfcs'])
FriendList = friend_functions.FLists()
NASCClient = friend_functions.NASCInteractor(identity)

weburl = "http://www.mechanicaldragon.xyz/part1dumper"

random_games =  [
    # Skylanders games
    0x0004000000165E00, 0x0004000000131200, 0x0004000000036E00, 0x0004000000091D00, 0x00040000000E6500,
    # Mama games
    0x000400000004E400
]


def update_presence():
    global RunSettings
    global random_games
    global NASCClient
    if datetime.utcnow() - RunSettings.LastGameChange > timedelta(seconds=Intervals.change_game):
        RunSettings.LastGameChange = datetime.utcnow()
        RunSettings.CurrentGame = random.choice(random_games)
    NASCClient.UpdatePresence(RunSettings.CurrentGame,'Domo Arigato')


class NotificationHandler(nintendo_notification.NintendoNotificationHandler):
    def __init__(self):
        self.name_cache = {}

    def process_notification_event(self, event):
        global FriendList
        if event.type == nintendo_notification.NotificationType.FRIEND_REQUEST_COMPLETE:
            p = friend_functions.process_friend.from_pid(event.pid)
            FriendList.newlfcs.put(p)
            logging.info("LFCS received for %s",friend_functions.FormattedFriendCode(p.fc))
            print("[",datetime.now(),"] LFCS received for",friend_functions.FormattedFriendCode(p.fc))
## Handle_LFCSQueue()
## iterate through lfcs queue and attempt to upload the data to the server
def Handle_LFCSQueue():
    global NASCClient, FriendList, Web
    while not FriendList.newlfcs.empty():
        p = FriendList.newlfcs.get()
        ## already added to lfcs queue
        if len([x for x in FriendList.lfcs if x.pid == p.pid]) > 0:
            continue
        FriendList.lfcs.append(p)
        FriendList.added = [x for x in FriendList.added if x.pid != p.pid]
        logging.info("LFCS processed for %s",friend_functions.FormattedFriendCode(p.fc))
        print("[",datetime.now(),"] LFCS processed for",friend_functions.FormattedFriendCode(p.fc))
    for x in FriendList.lfcs[:]:
        p = x
        FriendList.lfcs.remove(x)
        if p.lfcs is None:
            rel = NASCClient.RefreshFriendData(p.pid)
            if rel is None:
                FriendList.lfcs.append(p)
                continue
            p.lfcs=rel.friend_code
        if Web.UpdateLFCS(p.fc,p.lfcs) == False:
            logging.warning("LFCS failed to upload for %s",friend_functions.FormattedFriendCode(p.fc))
            print("[",datetime.now(),"] LFCS failed to uploaded for fc",friend_functions.FormattedFriendCode(p.fc))
            FriendList.lfcs.append(p)
            continue
        else:
            logging.info("LFCS uploaded successfully for %s",friend_functions.FormattedFriendCode(p.fc))
            print("[",datetime.now(),"] LFCS uploaded successfully for fc",friend_functions.FormattedFriendCode(p.fc))
            FriendList.remove.append(p.pid)
    return True

def Handle_FriendTimeouts():
    global FriendList, Web
    oldfriends = [x for x in FriendList.added if (datetime.utcnow()-timedelta(seconds=Intervals.friend_timeout)) > x.added_time]
    FriendList.added = [x for x in FriendList.added if (datetime.utcnow()-timedelta(seconds=Intervals.friend_timeout)) <= x.added_time]
    for x in oldfriends:
        logging.warning("Friend Code Timeout: %s",friend_functions.FormattedFriendCode(x.fc))
        print("[",datetime.now(),"] Friend code timeout:",friend_functions.FormattedFriendCode(x.fc))
        if Web.TimeoutFC(x.fc):
            FriendList.remove.append(x.pid)
        else:
            return False
    return True

def Handle_ReSync():
    global FriendList, NASCClient
    try:
        #print("[",datetime.now(),"] ReSync:",len(FriendList.added),"friends currently")
        for p in FriendList.added:
            if datetime.utcnow()-timedelta(seconds=Intervals.resync) < p.resync_time:
                continue
            time.sleep(Intervals.betweenNintendoActions)
            p.resync_time = datetime.utcnow() + timedelta(seconds = Intervals.resync)
            logging.info("ReSync: Checking friend for completion, refreshing: %s",friend_functions.FormattedFriendCode(p.fc))
            x = NASCClient.RefreshFriendData(p.pid)
            if x is None:
                continue
            if x.is_complete == True:
                p.lfcs = x.friend_code
                logging.info("ReSync: Friend was completed, adding to lfcs queue: %s",friend_functions.FormattedFriendCode(p.fc))
                print("[",datetime.now(),"] ReSync: Friend was completed, adding to lfcs queue:",friend_functions.FormattedFriendCode(p.fc))
                FriendList.newlfcs.put(p)
            else:
                logging.info("ReSync: Friend wasnt complete yet or is not in added friendlist: %s",friend_functions.FormattedFriendCode(p.fc))
    except Exception as e:
        print("[",datetime.now(),"] Got exception!!", e,"\n",sys.exc_info()[0].__name__, sys.exc_info()[2].tb_frame.f_code.co_filename, sys.exc_info()[2].tb_lineno)
        logging.error("Exception found: %s\n%s\n%s\n%s",e,sys.exc_info()[0].__name__, sys.exc_info()[2].tb_frame.f_code.co_filename, sys.exc_info()[2].tb_lineno)
        return False
    return True

def UnClaimAll():
    global Web, FriendList
    Handle_LFCSQueue()
    for x in FriendList.added[:]:
        logging.info("Attempting to unclaim: %s",friend_functions.FormattedFriendCode(x.fc))
        print ("Attempting to unclaim",friend_functions.FormattedFriendCode(x.fc))
        if Web.ResetFC(x.fc)==True:
            logging.info("Successfully unclaimed %s",friend_functions.FormattedFriendCode(x.fc))
            print ("Successfully unclaimed",friend_functions.FormattedFriendCode(x.fc))
            FriendList.added.remove(x)
            FriendList.remove.append(x.pid)
    for x in FriendList.notadded[:]:
        logging.info("Attempting to unclaim: %s",friend_functions.FormattedFriendCode(x))
        print ("Attempting to unclaim",friend_functions.FormattedFriendCode(x))
        if Web.ResetFC(x)==True:
            logging.info("Successfully unclaimed %s",friend_functions.FormattedFriendCode(x))
            print ("Successfully unclaimed",friend_functions.FormattedFriendCode(x))
            FriendList.notadded.remove(x)
            FriendList.remove.append(friend_functions.FC2PID(x))

def Handle_RemoveQueue():
    global NASCClient, FriendList
    for x in FriendList.remove[:]:
        time.sleep(Intervals.betweenNintendoActions)
        #pid = x
        resp = NASCClient.RemoveFriendPID(x)
        if resp==True:
            FriendList.remove.remove(x)
    return True

def HandleNewFriends():
    global FriendList, NASCClient
    FriendList.notadded = list(set(FriendList.notadded)) ## remove duplicates
    for fc in FriendList.notadded[:]:
    #while len(FriendList.notadded) > 0:
        curFriends = [x.fc for x in FriendList.added]
        curFriends.extend([x.fc for x in FriendList.lfcs])
        curFriends.extend([friend_functions.PID2FC(x) for x in FriendList.remove])
        #fc = FriendList.notadded[0]
        # remove from the actual list
        FriendList.notadded.remove(fc)
        # if not a valid friend, go to the next in the list
        if not friend_functions.is_valid_fc(fc):
            continue
        # if already on one of our lists, go to the next on the list
        if len([x for x in curFriends if x == fc]) > 0:
            continue
        logging.info("Adding friend %s",friend_functions.FormattedFriendCode(fc))
        print("[",datetime.now(),"] Adding friend:",friend_functions.FormattedFriendCode(fc))
        time.sleep(Intervals.betweenNintendoActions)
        #TODO error check this vvv
        rel = NASCClient.AddFriendFC(fc)
        if not rel is None:
            if rel.is_complete==True:
                logging.warning("NewFriends: Friend %s already completed, moving to LFCS list",friend_functions.FormattedFriendCode(fc))
                print("[",datetime.now(),"] NewFriends: Friend",friend_functions.FormattedFriendCode(fc),"already completed, moving to LFCS list")
                p = friend_functions.process_friend(fc)
                p.lfcs = rel.friend_code
                FriendList.lfcs.append(p)
                #added_friends = [x for x in added_friends if x.pid != p.pid]
            else:
                FriendList.added.append(friend_functions.process_friend(fc))


def sh_thread():
    global RunSettings, NASCClient, FriendList
    #print("Running bot as",myFriendCode[0:4]+"-"+myFriendCode[4:8]+"-"+myFriendCode[8:])
    while RunSettings.Running==True:
        try:
            
            if datetime.utcnow() < RunSettings.PauseUntil:
                continue
            if not Web.IsConnected():
                RunSettings.PauseUntil = datetime.utcnow()+timedelta(seconds=Intervals.error_wait)
                continue
            if NASCClient.Error() > 0:
                RunSettings.PauseUntil = datetime.utcnow()+timedelta(seconds=Intervals.nintendo_wait)
                UnClaimAll()
                RunSettings.ReconnectNintendo = True
                print("Nintendo Connection Failed, waiting",Intervals.nintendo_wait,"seconds")
                logging.error("Nintendo Connection Failed. Waiting %s seconds",Intervals.nintendo_wait)
                continue
            if RunSettings.ReconnectNintendo == True:
                NASCClient.reconnect()
                RunSettings.ReconnectNintendo = False

            clist = Web.getClaimedList()
            ## if the site doesnt have a fc as claimed, i shouldnt either
            ## unfriend anyone on my list that the website doesnt have for me
            FriendList.remove.extend([x.pid for x in FriendList.added if x.fc not in clist])
            ## remove the "others" from the added friends list
            FriendList.added = [x for x in FriendList.added if x.fc in clist]
            ## compare the claimed list with the current friends lists and add new friends to notadded
            addedfcs = [x.fc for x in FriendList.added]
            addedfcs.extend([x for x in FriendList.notadded])
            addedfcs.extend([x.fc for x in FriendList.lfcs])
            addedfcs.extend([friend_functions.PID2FC(x) for x in FriendList.remove])
            clist = [x for x in clist if not x in addedfcs and len(x)==12]
            if len(clist) > 0:
                logging.warning("%s friends already claimed, queued for adding", len(clist))
                print (len(clist)," friends already claimed, queued for adding")
            FriendList.notadded.extend(clist)
            ## Receives current relationship status for all friends, then iterates through them to set the lfcs status if not currently set
            time.sleep(Intervals.between_actions)
            logging.info("Resyncing friend list")
            Handle_ReSync()
            time.sleep(Intervals.between_actions)
            ## iterates through lfcs queue, uploads lfcs to website. returns false if the process fails somewhere
            if not Handle_LFCSQueue():
                logging.error("Could not completed LFCS queue processing")
                print("[",datetime.now(),"] could not complete LFCS queue processing")
            time.sleep(Intervals.between_actions)
            ## true if it makes it through the list, false otherwise
            if not Handle_FriendTimeouts():
                logging.error("Could not completed friend timeout processing")
                print("[",datetime.now(),"] could not Timeout old friends")
            time.sleep(Intervals.between_actions)
            ## iterates through removal queue, uploads lfcs to website. returns false if the process fails somewhere
            if not Handle_RemoveQueue():
                logging.error("Could not completed Remove queue processing")
                print("[",datetime.now(),"] Could not handle RemoveQueue")
                continue
            if datetime.utcnow() >= RunSettings.WaitForFriending:
                time.sleep(Intervals.between_actions)
                logging.info("Getting New FCs. Currently %s added, %s lfcs",len(FriendList.added),len(FriendList.lfcs))
                print("[",datetime.now(),"] Getting New FCs. Currently",len(FriendList.added),"added,",len(FriendList.lfcs),"lfcs")
                nlist = Web.getNewList()
                for x in nlist:
                    if Web.ClaimFC(x):
                        FriendList.notadded.append(x)
                RunSettings.WaitForFriending = datetime.utcnow()+timedelta(seconds=Intervals.get_friends)
            if len(FriendList.notadded) > 0:
                logging.info("%s new FCs to process", len(FriendList.notadded))
                print ("[",datetime.now(),"]",len(FriendList.notadded),"new friends")
            time.sleep(Intervals.between_actions)
            HandleNewFriends()


    
        except Exception as e:
            print("[",datetime.now(),"] Got exception!!", e,"\n",sys.exc_info()[0].__name__, sys.exc_info()[2].tb_frame.f_code.co_filename, sys.exc_info()[2].tb_lineno)
            logging.error("Exception found: %s\n%s\n%s\n%s",e,sys.exc_info()[0].__name__, sys.exc_info()[2].tb_frame.f_code.co_filename, sys.exc_info()[2].tb_lineno)
class ExitButton(npyscreen.ButtonPress):
    def whenPressed(self):
        self.parent.parentApp.switchForm(None)

class GetFriendsCheckBox(npyscreen.Checkbox):
    def whenToggled(self):
        global RunSettings, Web
        if self.value==True:
            a=1
        else:
            a=0
        RunSettings.active=a
        Web.SetActive(a)

class P1BotForm(npyscreen.FormBaseNew): 
    def while_waiting(self): 
        global FriendList, RunSettings, NASCClient
        #npyscreen.notify_wait("Update") 
        RunSettings.UpdateRunTime()
        self.lblRuntime.value = RunSettings.RunTime
        self.lblBotCount.value = str(RunSettings.BotterCount)
        self.lblMyFriendCode.value = friend_functions.FormattedFriendCode(RunSettings.friendcode)
        if RunSettings.active == 1:
            self.lblActive.value="True"
        else:
            self.lblActive.value = "False"
        flist = [friend_functions.FormattedFriendCode(x.fc) for x in FriendList.added]
        self.addedfriendslist.footer = "("+str(len(FriendList.added))+")"
        self.addedfriendslist.values = flist
        flist = [friend_functions.FormattedFriendCode(x.fc) for x in FriendList.lfcs]
        self.lfcslist.footer="("+str(len(FriendList.lfcs))+")"
        self.lfcslist.values = flist
        self.unfriendlist.footer="("+str(len(FriendList.remove))+")"
        flist = [friend_functions.FormattedFriendCode(friend_functions.PID2FC(x)) for x in FriendList.remove]
        self.unfriendlist.values = flist
        connected=NASCClient.IsConnected()
        if connected:
            self.lblConnected.value="Connected"
        else:
            self.lblConnected.value="Disconnected"
        self.getFriendsCB.value=RunSettings.active == 1
        self.display() 
    def create(self): 
        #self.date_widget = self.add(npyscreen.FixedText, value=datetime.now(), editable=False) 
        self.lblConnected  = self.add(npyscreen.TitleText, name = "Friend Service:",value=str(False),editable=False, use_two_lines=False,begin_entry_at=20 )
        self.nextrely -= 1
        self.nextrelx += 40
        self.lblBotCount  = self.add(npyscreen.TitleText, name = "BotCount:",value="0",editable=False, use_two_lines=False,begin_entry_at=20 )
        self.nextrely += 1
        self.nextrelx -= 40
        self.lblMyFriendCode  = self.add(npyscreen.TitleText, name = "My Friend Code:",value="",editable=False, use_two_lines=False,begin_entry_at=20 )
        self.nextrely -= 1
        self.nextrelx += 40
        self.lblActive  = self.add(npyscreen.TitleText, name = "Active:",value="True",editable=False, use_two_lines=False,begin_entry_at=20 )
        self.nextrelx -= 40
        self.nextrely += 1
        self.lblRuntime = self.add(npyscreen.TitleText, name = "Run Time:", value="0", editable=False,use_two_lines=False,begin_entry_at=20)
        self.nextrely += 1
        self.addedfriendslist = self.add(npyscreen.BoxTitle, name = "Friends", editable=False,height=15,width=25)
        self.nextrely -= 15
        self.nextrelx += 26
        self.lfcslist = self.add(npyscreen.BoxTitle, name = "LFCS Upload", editable=False,height=15,width=25)
        self.nextrely -= 15
        self.nextrelx += 26
        self.unfriendlist = self.add(npyscreen.BoxTitle, name = "Unfriend", editable=False,height=15,width=25)
        self.nextrely += 1
        self.nextrelx -= 52
        self.getFriendsCB = self.add(GetFriendsCheckBox, name="Get Friends", value=True)
        self.nextrely += 1
        self.nextrely += 1
        self.nextrely += 1
        self.exitButton = self.add(ExitButton, name="Exit")
        #self.how_exited_handers[npyscreen.wgwidget.EXITED_ESCAPE] = self.exit_application

class Part1Bot(npyscreen.NPSAppManaged):
    keypress_timeout_default = 10
    def onStart(self):
        P1Form = self.addForm("MAIN", P1BotForm, name="Part1Bot") 
    


print("Running system as",RunSettings.friendcode)

if RunSettings.UI == False:
    print("\n\n********** Type \'q\' and press enter to quit at any time **************\n\n")

Web = webhandler.WebsiteHandler(weburl,RunSettings.friendcode,RunSettings.active,RunSettings.version)
Web.ResetBotSettings()
NASCClient.connect()
NASCClient.SetNotificationHandler(NotificationHandler)

#all = client.get_all_friends()
## add current friends to list
flist = []
flist.extend(NASCClient.GetAllFriends())
for r in flist:
    p=friend_functions.process_friend.from_pid(r.principal_id,1200)
    if not r.is_complete == True:
        FriendList.added.append(p)
    else:
        p.lfcs = r.friend_code
        FriendList.lfcs.append(p)
RunSettings.CurrentGame = random.choice(random_games)
update_presence()


sh_thread_obj = threading.Thread(target=sh_thread)
sh_thread_obj.daemon = True
sh_thread_obj.start()

def presence_thread():
    global RunSettings
    while RunSettings.Running==True:
        time.sleep(30)
        if datetime.utcnow() < RunSettings.PauseUntil:
            continue
        update_presence()



def heartbeat_thread():
    global Web, NASCClient,RunSettings
    recwait = 0
    while RunSettings.Running==True:
        time.sleep(30)
        if datetime.utcnow() < RunSettings.PauseUntil:
            continue
        Web.SetActive(RunSettings.active)
        toggle,run = Web.GetBotSettings()
        if toggle==True:
            if RunSettings.active==1:
                RunSettings.active=0
            else:
                RunSettings.active=1
        Web.SetActive(RunSettings.active)
        if RunSettings.Running!=False:
            RunSettings.Running=run
        Web.getNewList()
        RunSettings.BotterCount=Web.BottersOnlineCount()

whb_thread_obj = threading.Thread(target=heartbeat_thread)
whb_thread_obj.daemon = True
whb_thread_obj.start()

p_thread_obj = threading.Thread(target=presence_thread)
p_thread_obj.daemon = True
p_thread_obj.start()

if RunSettings.UI==True:
    App = Part1Bot().run()
    RunSettings.Running=False
else:
    while RunSettings.Running==True:
        x=input("")
        x=x.lower()
        if x=='q' or x=='quit':
            RunSettings.Running = False
        if x=='a' or x=='active':
            if RunSettings.active==1:
                RunSettings.active=0
            else:
                RunSettings.active=1


print("Application quit initiated, closing")
sh_thread_obj.join()
print("Removing friends")
#print("added friends list,",len(added_friends))
#print("lfcs list,",len(lfcs_queue))
#print("remove friends list,",len(remove_queue))

rmlist = [x.fc for x in FriendList.added]
rmlist.extend([x.fc for x in FriendList.lfcs])
rmlist.extend([friend_functions.PID2FC(x) for x in FriendList.remove])

while len(rmlist) > 0:
    fc = rmlist[0]
    rmlist.pop(0)
    print("Removing",fc)
    if Web.ResetFC(fc)==True:
        time.sleep(Intervals.betweenNintendoActions)
        NASCClient.RemoveFriendFC(fc)
    else:
        rmlist.append(fc)
print("All Friends removed")
print("Disconnected NASC Client")
NASCClient.UpdatePresence(RunSettings.CurrentGame,"goodbyte",False)
NASCClient.disconnect
